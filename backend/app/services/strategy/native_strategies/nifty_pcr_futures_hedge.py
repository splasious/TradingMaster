"""
Nifty PCR Futures Hedge -- NIFTY futures with a short in-the-money option (native, unsandboxed)
================================================================================================

As agreed on 1-Oct-2026, made intraday with an MA trailing stop on 15-Oct-2026:

  PCR:      the Overall OI PCR -- the latest 15-minute record across the 4
            nearest weekly expiries (ctx.get_pcr: same session, at most 30
            minutes old). No record, no entry and no PCR exit: it waits.
  MA:       the average, over the last MA_CANDLES (8) completed 15-minute
            NIFTY 50 candles, of each candle's (open + high + low + close) / 4
            -- moving up a candle at each close, so it trails the trade.
  Bearish:  flat, PCR below 0.90 and NIFTY 50 below the MA -> short LOTS
            lots of the current-month NIFTY future and sell LOTS lots of a
            PUT. Both close when the PCR goes above 1.05, or a 15-minute
            NIFTY 50 candle closes above the MA (the trailing stop).
  Bullish:  flat, PCR above 1.10 and NIFTY 50 above the MA -> long LOTS lots
            of the current-month future and sell LOTS lots of a CALL. Both
            close when the PCR falls below 0.95, or a 15-minute candle
            closes below the MA (the trailing stop).
  Stop:     on the candle's close, not the live price (15-Oct-2026: a dip
            under the MA mid-candle closed a bullish trade at 14:24): a
            candle that completed after the entry, against the MA that
            includes it -- acted on when the candle comes in from Zerodha,
            within about a minute of its close.
  Re-entry: after the trailing stop, that side is entered again only once a
            15-minute candle completed after the stop closes back on its
            side of the MA (bullish: above it; bearish: below it), with the
            PCR and NIFTY 50 as for any entry (state["_ma_stop"], that day
            only). After a PCR exit there's no such wait.
  Flip:     an exit and the other side's entry can happen on the same check.
  Option:   the nearest weekly expiry after today -- on an expiry day, next
            week's -- and of its in-the-money strikes (a PUT above spot, a
            CALL below) the one whose premium is closest to Rs 150; when
            even the shallowest costs more, that one.
  Future:   the current month's; from the trading day before its expiry,
            next month's.
  Timing:   intraday. Entries 09:30-15:00 IST; at 15:00 everything is
            closed (a position found from an earlier day -- a 15:00 missed
            while stopped -- is closed at the first check). Entries and the
            PCR exit are checked every run (10 s), entries on the live
            NIFTY 50 price. Nothing is held overnight, so there are no
            rolls: the option never reaches its expiry day held, and the
            future is never held into its roll day.
  Size:     LOTS lots per leg at the contract's own lot size. Both legs or
            neither: no live price for either one, no entry.
  Risk:     short futures with a short ITM put pays at most about the
            premium and loses in a sharp rally (the bullish side the same in
            a fall) -- limited by the trailing stop and the 15:00 exit. With
            fewer than 8 candles, or NIFTY 50's candles behind (none newer
            than 30 minutes; at the open, the previous session's last), there
            is no MA: no entry, and only the PCR and 15:00 exits apply.
  Cash:     the future books only its profit or loss (native_runner.open_leg);
            the option's premium is credited when sold, debited when bought back.

Each exit is one closed trade with both legs. Runs through
services/paper_trading/native_runner.py (ctx.get_pcr, ctx.get_candles,
ctx.get_price(s), ctx.list_weekly_expiries, ctx.open_leg, ctx.close_leg,
ctx.record_trade, ctx.note, ctx.wake_at). Stop -> "Exit positions" closes
both legs at live prices (state["force_exit"]).
"""

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST
from app.services.market_data.nse_holidays import is_trading_holiday

UNDERLYING_SYMBOL = "NIFTY 50"
LOTS = 10
DEFAULT_LOT_SIZE = 65  # fallback only -- the contract's own lot_size is used when present
BEARISH_ENTRY, BEARISH_EXIT = 0.90, 1.05
BULLISH_ENTRY, BULLISH_EXIT = 1.10, 0.95
TARGET_PREMIUM = 150.0
ITM_STRIKES_PRICED = 20  # nearest in-the-money strikes looked at when picking one
ENTRY_START, SQUARE_OFF = time(9, 30), time(15, 0)  # entries from 09:30; everything closed at 15:00
MARKET_OPEN = time(9, 15)
MA_TIMEFRAME, MA_CANDLES = "15m", 8
BAR = timedelta(minutes=15)
MA_STALE_AFTER = timedelta(minutes=30)  # newest candle ended longer ago than this: no MA


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_trading_holiday(d)


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d


def futures_roll_day(expiry: date) -> date:
    """The trading day before the future's expiry."""
    return previous_trading_day(expiry)


def entry_bias(pcr: float) -> str | None:
    if pcr < BEARISH_ENTRY:
        return "bearish"
    if pcr > BULLISH_ENTRY:
        return "bullish"
    return None


def exit_due(bias: str, pcr: float) -> bool:
    return pcr > BEARISH_EXIT if bias == "bearish" else pcr < BULLISH_EXIT


def typical(bar: dict) -> float:
    return (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4


def moving_average(bars: list[dict]) -> float | None:
    """The MA of the last MA_CANDLES bars' (open + high + low + close) / 4."""
    if len(bars) < MA_CANDLES:
        return None
    return sum(typical(bar) for bar in bars[-MA_CANDLES:]) / MA_CANDLES


def ma_is_current(newest_end: datetime, now_ist: datetime) -> bool:
    """The newest candle ended at most MA_STALE_AFTER ago -- or, in the
    session's first half hour, it is the previous session's last."""
    newest_end = newest_end.astimezone(IST)
    if newest_end.date() == now_ist.date():
        return now_ist - newest_end <= MA_STALE_AFTER
    opening = datetime.combine(now_ist.date(), MARKET_OPEN, tzinfo=IST)
    return newest_end.date() == previous_trading_day(now_ist.date()) and now_ist - opening <= MA_STALE_AFTER


def ma_agrees(bias: str, price: float, ma: float) -> bool:
    """NIFTY on the trade's side of the MA: below it for bearish, above for bullish."""
    return price < ma if bias == "bearish" else price > ma


def ma_stop_due(bias: str, newest: dict, ma: float, opened_at: datetime) -> bool:
    """The trailing stop: a candle completed since the entry closed on the
    wrong side of the MA -- above it for bearish, below for bullish."""
    ended = as_aware_utc(newest["ts"]) + BAR
    return ended > opened_at and (newest["close"] > ma if bias == "bearish" else newest["close"] < ma)


def _candle_text(newest: dict | None, ma: float | None) -> str:
    if newest is None or ma is None:
        return ""
    ended = (as_aware_utc(newest["ts"]) + BAR).astimezone(IST)
    return f"{ended:%H:%M} close {newest['close']:,.1f} vs MA {ma:,.1f}"


def re_armed(stop: dict, newest: dict, ma: float) -> bool:
    """After the trailing stop: a candle completed since closes back on the side's side of the MA."""
    ended = as_aware_utc(newest["ts"]) + BAR
    return ended > datetime.fromisoformat(stop["at"]) and ma_agrees(stop["bias"], newest["close"], ma)


def pick_strike(candidates: list[tuple[float, float | None]], option_type: str, spot: float) -> tuple[float, float] | None:
    """(strike, premium) of the in-the-money candidate whose premium is
    closest to TARGET_PREMIUM -- the shallower on a tie. `candidates`:
    (strike, live premium or None)."""
    itm = [(k, p) for k, p in candidates if p and (k > spot if option_type == "PE" else k < spot)]
    if not itm:
        return None
    return min(itm, key=lambda kp: (abs(kp[1] - TARGET_PREMIUM), abs(kp[0] - spot)))


async def _current_future(ctx, underlying: Instrument, now_ist: datetime) -> Instrument | None:
    """The nearest NIFTY future whose roll day is still ahead -- so from its
    roll day on, an entry (and the roll itself) takes next month's."""
    futures = (
        await ctx.db.execute(
            select(Instrument)
            .where(
                Instrument.underlying_instrument_id == underlying.id, Instrument.instrument_type == "future",
                Instrument.expiry.is_not(None), Instrument.expiry >= now_ist.date(),
            )
            .order_by(Instrument.expiry)
        )
    ).scalars().all()
    return next((f for f in futures if now_ist.date() < futures_roll_day(f.expiry)), None)


async def _pick_option(ctx, underlying: Instrument, option_type: str, spot: float, today: date):
    """((instrument, premium), None), or (None, why not)."""
    expiries = await ctx.list_weekly_expiries(underlying.id, today, limit=3)
    expiry = next((e for e in expiries if e > today), None)
    if expiry is None:
        return None, "no NIFTY weekly expiry after today listed"
    stmt = select(Instrument).where(
        Instrument.underlying_instrument_id == underlying.id, Instrument.instrument_type == "option",
        Instrument.expiry == expiry, Instrument.option_type == option_type, Instrument.strike.is_not(None),
    )
    if option_type == "PE":
        stmt = stmt.where(Instrument.strike > spot).order_by(Instrument.strike.asc())
    else:
        stmt = stmt.where(Instrument.strike < spot).order_by(Instrument.strike.desc())
    candidates = (await ctx.db.execute(stmt.limit(ITM_STRIKES_PRICED))).scalars().all()
    if not candidates:
        return None, f"no in-the-money {expiry:%d %b} {option_type} listed"
    prices = await ctx.get_prices([c.id for c in candidates])
    picked = pick_strike([(c.strike, prices.get(c.id)) for c in candidates], option_type, spot)
    if picked is None:
        return None, f"no live premium for the {expiry:%d %b} {option_type}s yet"
    instrument = next(c for c in candidates if c.strike == picked[0])
    return (instrument, picked[1]), None


def _leg(instrument: Instrument, side: str, quantity: float, price: float, now: datetime) -> dict:
    return {
        "instrument_id": str(instrument.id), "symbol": instrument.symbol, "side": side, "quantity": quantity,
        "entry_price": price, "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
        "strike": instrument.strike, "option_type": instrument.option_type, "opened_at": now.isoformat(),
    }


def _leg_pnl(leg: dict, exit_price: float) -> float:
    move = (leg["entry_price"] - exit_price) if leg["side"] == "sell" else (exit_price - leg["entry_price"])
    return move * leg["quantity"]


async def _exit_price(ctx, leg: dict, instrument: Instrument | None, spot: float) -> float | None:
    """The leg's live price. One already past expiry (a missed roll) has
    none: an option settles at its intrinsic value, a future at the index."""
    price = await ctx.get_price(instrument.id) if instrument is not None else None
    expired = leg.get("expiry") and date.fromisoformat(leg["expiry"]) < ctx.now.astimezone(IST).date()
    if price is None and expired:
        if leg["option_type"] == "PE":
            price = max(leg["strike"] - spot, 0.0)
        elif leg["option_type"] == "CE":
            price = max(spot - leg["strike"], 0.0)
        else:
            price = spot
    return price


async def _close(ctx, leg: dict, instrument: Instrument, price: float) -> None:
    await ctx.close_leg(instrument, "buy" if leg["side"] == "sell" else "sell", leg["quantity"], price, entry_price=leg["entry_price"])


async def _record(ctx, closed: list[tuple[dict, float]], exit_reason: str) -> float:
    """One closed trade for these legs; P&L % of the future's contract value
    (of the premium, for an option alone)."""
    pnl = sum(_leg_pnl(leg, price) for leg, price in closed)
    futures_value = sum(leg["entry_price"] * leg["quantity"] for leg, _ in closed if not leg["option_type"])
    base = futures_value or sum(leg["entry_price"] * leg["quantity"] for leg, _ in closed)
    await ctx.record_trade(
        legs=[
            {
                "instrument_id": leg["instrument_id"], "side": "short" if leg["side"] == "sell" else "long",
                "quantity": leg["quantity"], "entry_price": leg["entry_price"], "exit_price": price,
            }
            for leg, price in closed
        ],
        pnl=pnl, pnl_pct=(pnl / base * 100) if base else 0.0, exit_reason=exit_reason,
        opened_at=min(datetime.fromisoformat(leg["opened_at"]) for leg, _ in closed),
    )
    return pnl


async def _ma(ctx, underlying: Instrument, now_ist: datetime) -> tuple[float | None, dict | None, str]:
    """(MA, the newest completed candle, why there's no MA)."""
    bars = await ctx.get_candles(underlying.id, MA_TIMEFRAME, MA_CANDLES)
    if len(bars) < MA_CANDLES:
        return None, None, f"fewer than {MA_CANDLES} NIFTY 15-min candles"
    newest = bars[-1]
    ended = as_aware_utc(newest["ts"]) + BAR
    if not ma_is_current(ended, now_ist):
        return None, newest, f"NIFTY 15-min candles behind (newest ended {ended.astimezone(IST):%d %b %H:%M})"
    return moving_average(bars), newest, ""


def _pcr_text(pcr: float | None) -> str:
    return f"PCR {pcr:.3f}" if pcr is not None else "no PCR record"


def _ma_text(spot: float, ma: float | None, why: str) -> str:
    return f"NIFTY {spot:,.1f} vs MA {ma:,.1f}" if ma is not None else f"no MA: {why}"


EXIT_TEXT = {"manual": "manual exit", "square_off_1500": "15:00 exit", "carried_overnight": "held from an earlier day", "ma_stop": "trailing stop"}


async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    today = now_ist.date()
    underlying = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))).scalar_one_or_none()
    if underlying is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return
    force_exit = ctx.state.pop("force_exit", False)
    position = ctx.state.get("position")
    spot = await ctx.get_price(underlying.id)
    if spot is None:
        ctx.note("skipped", reason="no live NIFTY price")
        return
    pcr = await ctx.get_pcr()
    ma, newest, ma_why = await _ma(ctx, underlying, now_ist)
    stop = ctx.state.get("_ma_stop")
    if stop is not None and datetime.fromisoformat(stop["at"]).astimezone(IST).date() != today:
        ctx.state.pop("_ma_stop")  # a stop holds back re-entry that day only
        stop = None
    for at in (datetime.combine(today, ENTRY_START, tzinfo=IST), datetime.combine(today, SQUARE_OFF, tzinfo=IST)):
        if now_ist < at:
            ctx.wake_at(at)  # the first entry check and the 15:00 exit on the dot
            break
    closed_note = None

    # --- Holding: 15:00 (or a position from an earlier day), the PCR exit,
    # the trailing stop -- the first that applies closes both legs. ----------
    if position is not None:
        legs = position["legs"]
        instruments = {name: await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"])) for name, leg in legs.items()}
        prices = {name: await _exit_price(ctx, leg, instruments[name], spot) for name, leg in legs.items()}
        bias = position["bias"]
        if force_exit:
            exit_reason = "manual"
        elif datetime.fromisoformat(position["opened_at"]).astimezone(IST).date() < today:
            exit_reason = "carried_overnight"
        elif now_ist.time() >= SQUARE_OFF:
            exit_reason = "square_off_1500"
        elif pcr is not None and exit_due(bias, pcr):
            exit_reason = f"pcr_above_{BEARISH_EXIT:.2f}" if bias == "bearish" else f"pcr_below_{BULLISH_EXIT:.2f}"
        elif ma is not None and ma_stop_due(bias, newest, ma, datetime.fromisoformat(position["opened_at"])):
            exit_reason = "ma_stop"
        else:
            exit_reason = None

        if exit_reason is not None:
            why = EXIT_TEXT.get(exit_reason) or _pcr_text(pcr)
            missing = [legs[n]["symbol"] for n in legs if instruments[n] is None or prices[n] is None]
            if missing:
                ctx.note("hold", reason=f"exit due ({why}) but no live price for {', '.join(missing)} -- next check")
                return
            for name, leg in legs.items():
                await _close(ctx, leg, instruments[name], prices[name])
            pnl = await _record(ctx, [(legs[n], prices[n]) for n in legs], exit_reason)
            ctx.state["position"] = position = None
            if exit_reason == "ma_stop":
                ctx.state["_ma_stop"] = stop = {"bias": bias, "at": ctx.now.isoformat()}
                why = f"trailing stop: {_candle_text(newest, ma)}"
            closed_note = f"closed {bias} ({why}): P&L {pnl:+,.0f}"
            if exit_reason in ("manual", "square_off_1500", "carried_overnight"):
                ctx.note("exited", signal="COVER", reason=closed_note)
                return
        else:
            target = f"exit above {BEARISH_EXIT:.2f}" if bias == "bearish" else f"exit below {BULLISH_EXIT:.2f}"
            stop_side = "stop on a 15-min close above it" if bias == "bearish" else "stop on a 15-min close below it"
            last = f" | last {_candle_text(newest, ma)}" if ma is not None else ""
            ctx.note("hold", reason=(
                f"{bias}: {legs['future']['symbol']} + short {legs['option']['symbol']} | {_pcr_text(pcr)} ({target}) | "
                f"{_ma_text(spot, ma, ma_why)} ({stop_side}){last} | out at {SQUARE_OFF:%H:%M}"
            ))
            return

    # --- Flat: enter if the PCR is in a zone and NIFTY is on its side of the MA.
    def flat(text: str) -> None:
        ctx.note("exited" if closed_note else "skipped", signal="COVER" if closed_note else None,
                 reason=f"{closed_note} | {text}" if closed_note else text)

    if not ENTRY_START <= now_ist.time() < SQUARE_OFF:
        flat(f"flat | {_pcr_text(pcr)} | entries {ENTRY_START:%H:%M}-{SQUARE_OFF:%H:%M} IST")
        return
    if pcr is None:
        flat("flat | no PCR record yet")
        return
    bias = entry_bias(pcr)
    if bias is None:
        flat(f"flat | PCR {pcr:.3f} -- enters below {BEARISH_ENTRY:.2f} (NIFTY under the MA) or above {BULLISH_ENTRY:.2f} (over it)")
        return
    if ma is None:
        flat(f"flat | PCR {pcr:.3f}: {bias} waits -- no MA: {ma_why}")
        return
    side = "below" if bias == "bearish" else "above"
    if not ma_agrees(bias, spot, ma):
        flat(f"flat | PCR {pcr:.3f}: {bias} waits for NIFTY {side} the MA | {_ma_text(spot, ma, ma_why)}")
        return
    if stop is not None and stop["bias"] == bias and not re_armed(stop, newest, ma):
        stopped = datetime.fromisoformat(stop["at"]).astimezone(IST)
        flat(f"flat | PCR {pcr:.3f}: {bias} stopped out at {stopped:%H:%M} -- waits for a 15-min close {side} the MA | "
             f"{_ma_text(spot, ma, ma_why)}")
        return
    future = await _current_future(ctx, underlying, now_ist)
    if future is None:
        flat(f"{bias} entry: no current NIFTY future listed")
        return
    option_type = "PE" if bias == "bearish" else "CE"
    picked, why = await _pick_option(ctx, underlying, option_type, spot, today)
    if picked is None:
        flat(f"{bias} entry: {why}")
        return
    option, premium = picked
    future_price = await ctx.get_price(future.id)
    if future_price is None:
        flat(f"{bias} entry: no live price for {future.symbol} yet")
        return

    future_side = "sell" if bias == "bearish" else "buy"
    future_qty = float(LOTS * (future.lot_size or DEFAULT_LOT_SIZE))
    option_qty = float(LOTS * (option.lot_size or DEFAULT_LOT_SIZE))
    await ctx.open_leg(future, future_side, future_qty, future_price)
    await ctx.open_leg(option, "sell", option_qty, premium)
    ctx.state.pop("_ma_stop", None)
    ctx.state["position"] = {
        "bias": bias, "opened_at": ctx.now.isoformat(), "pcr_at_entry": pcr, "spot_at_entry": spot, "ma_at_entry": round(ma, 2),
        "legs": {
            "future": _leg(future, future_side, future_qty, future_price, ctx.now),
            "option": _leg(option, "sell", option_qty, premium, ctx.now),
        },
    }
    opened = (
        f"{bias} (PCR {pcr:.3f}): {'short' if bias == 'bearish' else 'long'} {future.symbol} @ {future_price:.2f}, "
        f"sold {option.symbol} @ {premium:.2f} ({abs(option.strike - spot):.0f} pts ITM) | {_ma_text(spot, ma, ma_why)}"
    )
    ctx.note("entered", signal="SHORT_FUT_PE" if bias == "bearish" else "LONG_FUT_CE",
             reason=f"{closed_note} | {opened}" if closed_note else opened)
