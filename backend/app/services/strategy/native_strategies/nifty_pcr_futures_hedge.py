"""
Nifty PCR Futures Hedge -- NIFTY futures with a short in-the-money option (native, unsandboxed)
================================================================================================

As agreed on 1-Oct-2026:

  PCR:      the Overall OI PCR -- the latest 15-minute record across the 4
            nearest weekly expiries (ctx.get_pcr: same session, at most 30
            minutes old). No record, no entry and no exit: it waits.
  Bearish:  flat and PCR below 0.75 -> short LOTS lots of the current-month
            NIFTY future and sell LOTS lots of a PUT. Both close when the
            PCR goes above 0.80.
  Bullish:  flat and PCR above 1.25 -> long LOTS lots of the current-month
            future and sell LOTS lots of a CALL. Both close when the PCR
            falls below 1.20.
  Flip:     an exit and the other side's entry can happen on the same check.
  Option:   the nearest weekly expiry after today -- on an expiry day, next
            week's -- and of its in-the-money strikes (a PUT above spot, a
            CALL below) the one whose premium is closest to Rs 150; when
            even the shallowest costs more, that one.
  Rolls:    the option at 09:20 on its expiry day, to next week's (same
            strike rule); the future at 15:00 one trading day before its
            monthly expiry, to the next month's (same side and size). An
            entry from that day on takes next month's future.
  Timing:   entries 09:45-15:00 IST; exits and rolls whenever the market is
            open; positions are held overnight -- there is no square-off.
  Size:     LOTS lots per leg at the contract's own lot size. Both legs or
            neither: no live price for either one, no entry.
  Risk:     no stop-loss. Short futures with a short ITM put pays at most
            about the premium and loses without limit in a sharp rally (and
            the bullish side the same in a fall); only the PCR exits close.
  Cash:     the future books only its profit or loss (native_runner.open_leg);
            the option's premium is credited when sold, debited when bought back.

Each exit is one closed trade with both legs; each roll is one closed trade
for the leg rolled. Runs through services/paper_trading/native_runner.py
(ctx.get_pcr, ctx.get_price(s), ctx.list_weekly_expiries, ctx.open_leg,
ctx.close_leg, ctx.record_trade, ctx.note). Stop -> "Exit positions" closes
both legs at live prices (state["force_exit"]).
"""

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST
from app.services.market_data.nse_holidays import is_trading_holiday

UNDERLYING_SYMBOL = "NIFTY 50"
LOTS = 10
DEFAULT_LOT_SIZE = 65  # fallback only -- the contract's own lot_size is used when present
BEARISH_ENTRY, BEARISH_EXIT = 0.75, 0.80
BULLISH_ENTRY, BULLISH_EXIT = 1.25, 1.20
TARGET_PREMIUM = 150.0
ITM_STRIKES_PRICED = 20  # nearest in-the-money strikes looked at when picking one
ENTRY_START, ENTRY_END = time(9, 45), time(15, 0)
OPTION_ROLL_AT = time(9, 20)  # on the weekly option's expiry day
FUTURES_ROLL_AT = time(15, 0)  # one trading day before the monthly expiry


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_trading_holiday(d)


def futures_roll_day(expiry: date) -> date:
    """The trading day before the future's expiry."""
    d = expiry - timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d


def entry_bias(pcr: float) -> str | None:
    if pcr < BEARISH_ENTRY:
        return "bearish"
    if pcr > BULLISH_ENTRY:
        return "bullish"
    return None


def exit_due(bias: str, pcr: float) -> bool:
    return pcr > BEARISH_EXIT if bias == "bearish" else pcr < BULLISH_EXIT


def option_roll_due(expiry: date, now_ist: datetime) -> bool:
    """From 09:20 on the option's expiry day -- or any time after it, if
    that day was missed (stopped, logged out)."""
    return expiry < now_ist.date() or (expiry == now_ist.date() and now_ist.time() >= OPTION_ROLL_AT)


def futures_roll_due(expiry: date, now_ist: datetime) -> bool:
    roll_day = futures_roll_day(expiry)
    return now_ist.date() > roll_day or (now_ist.date() == roll_day and now_ist.time() >= FUTURES_ROLL_AT)


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


async def _roll_option(ctx, position: dict, instrument, price, underlying, spot: float, today: date) -> str:
    leg = position["legs"]["option"]
    if instrument is None or price is None:
        return f"option roll due, no price for {leg['symbol']} yet"
    picked, why = await _pick_option(ctx, underlying, leg["option_type"], spot, today)
    if picked is None:
        return f"option roll due: {why}"
    new, premium = picked
    await _close(ctx, leg, instrument, price)
    await _record(ctx, [(leg, price)], "option_rollover")
    quantity = float(LOTS * (new.lot_size or DEFAULT_LOT_SIZE))
    await ctx.open_leg(new, "sell", quantity, premium)
    position["legs"]["option"] = _leg(new, "sell", quantity, premium, ctx.now)
    return f"rolled {leg['symbol']} @ {price:.2f} -> {new.symbol} @ {premium:.2f}"


async def _roll_future(ctx, position: dict, instrument, price, underlying, now_ist: datetime) -> str:
    leg = position["legs"]["future"]
    if instrument is None or price is None:
        return f"futures roll due, no price for {leg['symbol']} yet"
    new = await _current_future(ctx, underlying, now_ist)
    if new is None or str(new.id) == leg["instrument_id"]:
        return "futures roll due: next month's NIFTY future isn't listed"
    new_price = await ctx.get_price(new.id)
    if new_price is None:
        return f"futures roll due: no live price for {new.symbol} yet"
    await _close(ctx, leg, instrument, price)
    await _record(ctx, [(leg, price)], "futures_rollover")
    quantity = float(LOTS * (new.lot_size or DEFAULT_LOT_SIZE))
    await ctx.open_leg(new, leg["side"], quantity, new_price)
    position["legs"]["future"] = _leg(new, leg["side"], quantity, new_price, ctx.now)
    return f"rolled {leg['symbol']} @ {price:.2f} -> {new.symbol} @ {new_price:.2f}"


def _pcr_text(pcr: float | None) -> str:
    return f"PCR {pcr:.3f}" if pcr is not None else "no PCR record"


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
    closed_note = None

    # --- Holding: exit, else roll whatever is due. ---------------------------
    if position is not None:
        legs = position["legs"]
        instruments = {name: await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"])) for name, leg in legs.items()}
        prices = {name: await _exit_price(ctx, leg, instruments[name], spot) for name, leg in legs.items()}
        bias = position["bias"]

        if force_exit or (pcr is not None and exit_due(bias, pcr)):
            missing = [legs[n]["symbol"] for n in legs if instruments[n] is None or prices[n] is None]
            if missing:
                ctx.note("hold", reason=f"exit due ({_pcr_text(pcr)}) but no live price for {', '.join(missing)} -- next check")
                return
            for name, leg in legs.items():
                await _close(ctx, leg, instruments[name], prices[name])
            reason = "manual" if force_exit else (f"pcr_above_{BEARISH_EXIT:.2f}" if bias == "bearish" else f"pcr_below_{BULLISH_EXIT:.2f}")
            pnl = await _record(ctx, [(legs[n], prices[n]) for n in legs], reason)
            ctx.state["position"] = position = None
            closed_note = f"closed {bias} ({'manual exit' if force_exit else _pcr_text(pcr)}): P&L {pnl:+,.0f}"
            if force_exit:
                ctx.note("exited", signal="COVER", reason=closed_note)
                return
        else:
            rolls = []
            if option_roll_due(date.fromisoformat(legs["option"]["expiry"]), now_ist):
                rolls.append(await _roll_option(ctx, position, instruments["option"], prices["option"], underlying, spot, today))
            if futures_roll_due(date.fromisoformat(legs["future"]["expiry"]), now_ist):
                rolls.append(await _roll_future(ctx, position, instruments["future"], prices["future"], underlying, now_ist))
            ctx.state["position"] = position
            target = f"exit above {BEARISH_EXIT:.2f}" if bias == "bearish" else f"exit below {BULLISH_EXIT:.2f}"
            reason = f"{bias}: {legs['future']['symbol']} + short {legs['option']['symbol']} | {_pcr_text(pcr)} ({target})"
            if rolls:
                ctx.note("rolled" if any(r.startswith("rolled") for r in rolls) else "hold", signal="ROLL", reason=f"{' | '.join(rolls)} | {reason}")
            else:
                ctx.note("hold", reason=reason)
            return

    # --- Flat: enter if the PCR is in a zone. ---------------------------------
    def flat(text: str) -> None:
        ctx.note("exited" if closed_note else "skipped", signal="COVER" if closed_note else None,
                 reason=f"{closed_note} | {text}" if closed_note else text)

    if not ENTRY_START <= now_ist.time() < ENTRY_END:
        flat(f"flat | {_pcr_text(pcr)} | entries {ENTRY_START:%H:%M}-{ENTRY_END:%H:%M} IST")
        return
    if pcr is None:
        flat("flat | no PCR record yet")
        return
    bias = entry_bias(pcr)
    if bias is None:
        flat(f"flat | PCR {pcr:.3f} -- enters below {BEARISH_ENTRY} or above {BULLISH_ENTRY}")
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
    ctx.state["position"] = {
        "bias": bias, "opened_at": ctx.now.isoformat(), "pcr_at_entry": pcr, "spot_at_entry": spot,
        "legs": {
            "future": _leg(future, future_side, future_qty, future_price, ctx.now),
            "option": _leg(option, "sell", option_qty, premium, ctx.now),
        },
    }
    opened = (
        f"{bias} (PCR {pcr:.3f}): {'short' if bias == 'bearish' else 'long'} {future.symbol} @ {future_price:.2f}, "
        f"sold {option.symbol} @ {premium:.2f} ({abs(option.strike - spot):.0f} pts ITM)"
    )
    ctx.note("entered", signal="SHORT_FUT_PE" if bias == "bearish" else "LONG_FUT_CE",
             reason=f"{closed_note} | {opened}" if closed_note else opened)
