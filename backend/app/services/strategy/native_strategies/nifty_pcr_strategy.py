"""
NIFTY PCR Strategy -- TOTAL_OI_PCR state machine on completed 15-minute signals (native, unsandboxed)
=====================================================================================================

The user's NIFTY_PCR_STRATEGY.py (2 Oct 2026), run by the platform's paper
engine (services/paper_trading/native_runner.py) instead of its own
BrokerAdapter loop.

  TOTAL_OI_PCR  sum(PE OI) / sum(CE OI) over the nearest weekly expiry,
                ATM +/- PCR_STRIKES_EACH_SIDE strikes (STRIKE_STEP apart,
                ATM = spot rounded to the step). Read from the platform's
                15-minute PCR record for that close (pcr_snapshots: the OI
                of every strike at the mark); a strike without both OIs is
                left out, as in the file. A record with fewer than
                PCR_MIN_COVERAGE of the window's strikes priced, or with no
                call OI, is not acted on (the file's stale-data block).
  Signals       each completed 15-minute close from 09:45 to 15:15 IST,
                once, when its record has arrived (normally seconds after
                the close). PCR entries and exits happen only here. A run
                acts only on closes after it started (v2): switched on at
                10:52, its first signal is 11:00.
  FLAT          PCR > 1.25 -> BULLISH; PCR < 0.75 -> BEARISH;
                0.80 <= PCR <= 1.20 -> NEUTRAL (unless locked for the day);
                otherwise stay flat (transition band).
  BULLISH       long LOTS lots of the current-month NIFTY future + short
                LOTS lots of the CE whose premium is closest to Rs 150
                among ATM +/- OPTION_PREMIUM_SEARCH_STRIKES (ties: nearer
                spot). Exits (both legs) when PCR < 1.20.
  BEARISH       short future + short PE near Rs 150 the same way. Exits
                when PCR > 0.80.
  NEUTRAL       short CE + short PE at the same strike: of ATM-50, ATM,
                ATM+50 the one nearest spot (ties: the bigger combined
                premium), nearest weekly expiry -- on an expiry day, that
                day's. Exits when PCR < 0.75 or > 1.25. Checked every cycle
                (not only at signals): spot STRADDLE_SHIFT_POINTS from the
                reference -> closed and re-opened at the new ATM (new
                reference); at 15:10 closed, and no new neutral entry that
                day. Bullish and bearish positions are never closed at 15:10.
  An exit leaves the strategy FLAT; the next signal may open a regime.
  Holding       BULLISH and BEARISH are held overnight.
  Rolls         (BULLISH/BEARISH only) On the short option's expiry day: at a
                signal where PCR is still past the entry level (> 1.25 /
                < 0.75), buy it back and sell next week's near Rs 150; and
                -- agreed 2 Oct -- if it still hasn't rolled at the 15:00
                close (15:05 if that record is late), roll it then anyway.
                On the future's expiry day: at the first signal it's still
                held (PCR >= 1.20 bullish / <= 0.80 bearish -- always true
                once the exit check passed), close it and open the next
                month's, same side and size; 15:05 if no record came.
                A leg found already expired (the strategy was stopped over
                its expiry) is rolled at the next cycle, closed at its
                settlement value (intrinsic for an option, spot for the
                future).
  Expiry day    an entry takes next week's option / next month's future
                directly on their expiry day -- the file opens the expiring
                contract and rolls it in the same cycle; same position,
                half the orders.
  Size          LOTS lots per leg at the contract's own lot size.

Kept from the file: no PCR action before 09:45 or after 15:15; the 15:10
neutral lock survives exits; a regime is entered only when flat. A neutral
reading at the 15:15 close doesn't open a straddle: the 15:10 rule would
close it at once.
Left out (a paper engine fills immediately): order retries, the margin
check, DRY_RUN; MAX_DAILY_LOSS is None in the file, so no daily-loss halt.

Stop -> "Exit positions" closes every leg at live prices (state["force_exit"]).
Each exit, recenter and roll is recorded as a closed trade.
"""

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.models.instrument import Instrument
from app.models.pcr import PcrSnapshot, PcrStrikeOi
from app.services.broker.zerodha_broker import IST
from app.services.market_data.hours import nse_market_open
from app.services.options.pcr_snapshots import latest_mark

VERSION = 2

UNDERLYING_SYMBOL = "NIFTY 50"  # the index in the chart catalog
PCR_UNDERLYING = "NIFTY"  # the PCR records' name for it
LOTS = 10
DEFAULT_LOT_SIZE = 65  # fallback only -- the contract's own lot_size is used when present
OPTION_TARGET_PREMIUM = 150.0
OPTION_PREMIUM_SEARCH_STRIKES = 20
PCR_STRIKES_EACH_SIDE = 20
STRIKE_STEP = 50
STRADDLE_SHIFT_POINTS = 100.0

BULL_ENTRY_PCR, BULL_EXIT_PCR = 1.25, 1.20
BEAR_ENTRY_PCR, BEAR_EXIT_PCR = 0.75, 0.80
NEUTRAL_ENTRY_LOW, NEUTRAL_ENTRY_HIGH = 0.80, 1.20
NEUTRAL_EXIT_LOW, NEUTRAL_EXIT_HIGH = 0.75, 1.25

FIRST_SIGNAL_TIME = time(9, 45)
LAST_SIGNAL_TIME = time(15, 15)
STRADDLE_FORCE_EXIT = time(15, 10)
EXPIRY_ROLL_BY = time(15, 0)  # agreed 2 Oct: an option not rolled by the 15:00 close rolls then
LATE_RECORD_GRACE = timedelta(minutes=5)  # ... or at 15:05 if the 15:00 record never came
PCR_MIN_COVERAGE = 0.9

FLAT, BULLISH, BEARISH, NEUTRAL = "FLAT", "BULLISH", "BEARISH", "NEUTRAL"


# ------------------------------------------------------------------ rules --

def atm_strike(spot: float) -> float:
    """Spot to the nearest STRIKE_STEP, a tie rounding up."""
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def strike_grid(spot: float, each_side: int) -> list[float]:
    atm = atm_strike(spot)
    return [atm + i * STRIKE_STEP for i in range(-each_side, each_side + 1)]


def entry_regime(pcr: float) -> str | None:
    if pcr > BULL_ENTRY_PCR:
        return BULLISH
    if pcr < BEAR_ENTRY_PCR:
        return BEARISH
    if NEUTRAL_ENTRY_LOW <= pcr <= NEUTRAL_ENTRY_HIGH:
        return NEUTRAL
    return None


def exit_due(regime: str, pcr: float) -> bool:
    if regime == BULLISH:
        return pcr < BULL_EXIT_PCR
    if regime == BEARISH:
        return pcr > BEAR_EXIT_PCR
    if regime == NEUTRAL:
        return pcr < NEUTRAL_EXIT_LOW or pcr > NEUTRAL_EXIT_HIGH
    return False


def past_entry_level(regime: str, pcr: float) -> bool:
    """The file's condition for rolling the option at a signal."""
    return (regime == BULLISH and pcr > BULL_ENTRY_PCR) or (regime == BEARISH and pcr < BEAR_ENTRY_PCR)


def signal_mark(now: datetime) -> datetime | None:
    """The completed 15-minute close a signal is due for now: the latest
    mark at or before `now`, today, between 09:45 and 15:15 IST."""
    mark = latest_mark(now)
    if mark is None:
        return None
    mark_ist = mark.astimezone(IST)
    if mark_ist.date() != now.astimezone(IST).date() or not FIRST_SIGNAL_TIME <= mark_ist.time() <= LAST_SIGNAL_TIME:
        return None
    return mark


def total_oi_pcr(rows: list[tuple[float, str, float | None]], spot: float) -> tuple[float | None, str]:
    """TOTAL_OI_PCR over ATM +/- PCR_STRIKES_EACH_SIDE from (strike, CE|PE,
    OI) rows of the nearest expiry: (pcr, detail) or (None, why not)."""
    grid = set(strike_grid(spot, PCR_STRIKES_EACH_SIDE))
    by_strike: dict[float, dict[str, float | None]] = {}
    for strike, option_type, oi in rows:
        if strike in grid:
            by_strike.setdefault(strike, {})[option_type] = oi
    listed = len(by_strike)
    put_oi = call_oi = 0.0
    used = 0
    for sides in by_strike.values():
        ce, pe = sides.get("CE"), sides.get("PE")
        if ce is None or pe is None:
            continue
        call_oi += max(0.0, ce)
        put_oi += max(0.0, pe)
        used += 1
    if not listed or used < PCR_MIN_COVERAGE * listed:
        return None, f"only {used} of {listed} strikes have both OIs"
    if call_oi <= 0:
        return None, "call OI is zero"
    return put_oi / call_oi, f"{used} strikes"


def pick_near_premium(candidates: list[tuple[float, float | None]], spot: float) -> tuple[float, float] | None:
    """(strike, premium) closest to OPTION_TARGET_PREMIUM, ties to the strike
    nearer spot; `candidates`: (strike, live premium or None)."""
    priced = [(k, p) for k, p in candidates if p and p > 0]
    if not priced:
        return None
    return min(priced, key=lambda kp: (abs(kp[1] - OPTION_TARGET_PREMIUM), abs(kp[0] - spot)))


def pick_straddle(candidates: list[tuple[float, float | None, float | None]], spot: float) -> tuple[float, float, float] | None:
    """(strike, CE, PE) of ATM-50/ATM/ATM+50 nearest spot, ties to the bigger
    combined premium; `candidates`: (strike, CE premium, PE premium)."""
    priced = [(k, ce, pe) for k, ce, pe in candidates if ce and pe and ce > 0 and pe > 0]
    if not priced:
        return None
    return min(priced, key=lambda c: (abs(c[0] - spot), -(c[1] + c[2])))


# --------------------------------------------------------------- contracts --

async def _expiries(ctx, underlying: Instrument, today: date) -> list[date]:
    return await ctx.list_weekly_expiries(underlying.id, today, limit=4)


async def _option_expiry(ctx, underlying: Instrument, today: date, skip_today: bool, after: date | None = None) -> date | None:
    for expiry in await _expiries(ctx, underlying, today):
        if (skip_today and expiry == today) or (after is not None and expiry <= after):
            continue
        return expiry
    return None


async def _future(ctx, underlying: Instrument, today: date, after: date | None = None) -> Instrument | None:
    """The nearest NIFTY future still trading after today (on an expiry day,
    next month's), or the first one after `after`."""
    stmt = select(Instrument).where(
        Instrument.underlying_instrument_id == underlying.id, Instrument.instrument_type == "future",
        Instrument.expiry.is_not(None), Instrument.expiry > (after if after and after > today else today),
    ).order_by(Instrument.expiry)
    return (await ctx.db.execute(stmt.limit(1))).scalar_one_or_none()


async def _options(ctx, underlying: Instrument, expiry: date, option_type: str, strikes: list[float]) -> dict[float, Instrument]:
    rows = (
        await ctx.db.execute(
            select(Instrument).where(
                Instrument.underlying_instrument_id == underlying.id, Instrument.instrument_type == "option",
                Instrument.expiry == expiry, Instrument.option_type == option_type, Instrument.strike.in_(strikes),
            )
        )
    ).scalars().all()
    return {float(r.strike): r for r in rows}


async def _option_near_premium(ctx, underlying: Instrument, option_type: str, expiry: date, spot: float):
    """((instrument, premium), None) or (None, why not)."""
    options = await _options(ctx, underlying, expiry, option_type, strike_grid(spot, OPTION_PREMIUM_SEARCH_STRIKES))
    if not options:
        return None, f"no {expiry:%d %b} {option_type} listed near spot"
    prices = await ctx.get_prices([o.id for o in options.values()])
    picked = pick_near_premium([(k, prices.get(o.id)) for k, o in options.items()], spot)
    if picked is None:
        return None, f"no live premium for the {expiry:%d %b} {option_type}s yet"
    return (options[picked[0]], picked[1]), None


async def _straddle(ctx, underlying: Instrument, today: date, spot: float):
    """((expiry, strike, ce, ce_price, pe, pe_price), None) or (None, why not)."""
    expiry = await _option_expiry(ctx, underlying, today, skip_today=False)
    if expiry is None:
        return None, "no NIFTY weekly expiry listed"
    atm = atm_strike(spot)
    strikes = [atm - STRIKE_STEP, atm, atm + STRIKE_STEP]
    ces = await _options(ctx, underlying, expiry, "CE", strikes)
    pes = await _options(ctx, underlying, expiry, "PE", strikes)
    prices = await ctx.get_prices([o.id for o in list(ces.values()) + list(pes.values())])
    picked = pick_straddle(
        [(k, prices.get(ces[k].id) if k in ces else None, prices.get(pes[k].id) if k in pes else None) for k in strikes], spot,
    )
    if picked is None:
        return None, f"no priced {expiry:%d %b} straddle near {atm:.0f} yet"
    strike, ce_price, pe_price = picked
    return (expiry, strike, ces[strike], ce_price, pes[strike], pe_price), None


# --------------------------------------------------------------- the book --

def _qty(instrument: Instrument) -> float:
    return float(LOTS * (instrument.lot_size or DEFAULT_LOT_SIZE))


def _leg(instrument: Instrument, side: str, quantity: float, price: float, now: datetime) -> dict:
    return {
        "instrument_id": str(instrument.id), "symbol": instrument.symbol, "side": side, "quantity": quantity,
        "entry_price": price, "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
        "strike": instrument.strike, "option_type": instrument.option_type, "opened_at": now.isoformat(),
    }


def _leg_pnl(leg: dict, exit_price: float) -> float:
    move = (leg["entry_price"] - exit_price) if leg["side"] == "sell" else (exit_price - leg["entry_price"])
    return move * leg["quantity"]


def _expired(leg: dict, today: date) -> bool:
    return bool(leg.get("expiry")) and date.fromisoformat(leg["expiry"]) < today


async def _exit_price(ctx, leg: dict, spot: float, today: date) -> float | None:
    """The leg's live price; one already past expiry settles at its
    intrinsic value (an option) or the index (the future)."""
    if _expired(leg, today):
        if leg["option_type"] == "PE":
            return max(leg["strike"] - spot, 0.0)
        if leg["option_type"] == "CE":
            return max(spot - leg["strike"], 0.0)
        return spot
    return await ctx.get_price(uuid.UUID(leg["instrument_id"]))


async def _close_legs(ctx, legs: list[dict], prices: list[float], exit_reason: str) -> float:
    for leg, price in zip(legs, prices):
        instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        await ctx.close_leg(instrument, "buy" if leg["side"] == "sell" else "sell", leg["quantity"], price, entry_price=leg["entry_price"])
    pnl = sum(_leg_pnl(leg, price) for leg, price in zip(legs, prices))
    futures_value = sum(leg["entry_price"] * leg["quantity"] for leg in legs if not leg["option_type"])
    base = futures_value or sum(leg["entry_price"] * leg["quantity"] for leg in legs)
    await ctx.record_trade(
        legs=[
            {"instrument_id": leg["instrument_id"], "side": "short" if leg["side"] == "sell" else "long", "quantity": leg["quantity"],
             "entry_price": leg["entry_price"], "exit_price": price}
            for leg, price in zip(legs, prices)
        ],
        pnl=pnl, pnl_pct=(pnl / base * 100) if base else 0.0, exit_reason=exit_reason,
        opened_at=min(datetime.fromisoformat(leg["opened_at"]) for leg in legs),
    )
    return pnl


async def _flatten(ctx, state: dict, spot: float, today: date, exit_reason: str) -> str | None:
    """Closes every leg; None when done, else why not (a leg without a price)."""
    legs = list(state.get("legs", {}).values())
    if legs:
        prices = [await _exit_price(ctx, leg, spot, today) for leg in legs]
        missing = [leg["symbol"] for leg, price in zip(legs, prices) if price is None]
        if missing:
            return f"no live price for {', '.join(missing)}"
        pnl = await _close_legs(ctx, legs, prices, exit_reason)
        state["last_exit"] = f"{state.get('regime')} closed ({exit_reason}): P&L {pnl:+,.0f}"
    state.update({"regime": FLAT, "legs": {}, "reference_spot": None, "straddle_strike": None, "opened_at": None})
    return None


async def _enter_directional(ctx, state: dict, underlying: Instrument, regime: str, spot: float, pcr: float, today: date) -> str | None:
    future = await _future(ctx, underlying, today)
    if future is None:
        return "no current NIFTY future listed"
    expiry = await _option_expiry(ctx, underlying, today, skip_today=True)
    if expiry is None:
        return "no NIFTY weekly expiry after today listed"
    option_type = "CE" if regime == BULLISH else "PE"
    picked, why = await _option_near_premium(ctx, underlying, option_type, expiry, spot)
    if picked is None:
        return why
    option, premium = picked
    future_price = await ctx.get_price(future.id)
    if future_price is None:
        return f"no live price for {future.symbol} yet"
    future_side = "buy" if regime == BULLISH else "sell"
    await ctx.open_leg(future, future_side, _qty(future), future_price)
    await ctx.open_leg(option, "sell", _qty(option), premium)
    state.update({
        "regime": regime, "opened_at": ctx.now.isoformat(), "pcr_at_entry": pcr, "reference_spot": None, "straddle_strike": None,
        "legs": {
            "future": _leg(future, future_side, _qty(future), future_price, ctx.now),
            "option": _leg(option, "sell", _qty(option), premium, ctx.now),
        },
    })
    return None


async def _enter_neutral(ctx, state: dict, underlying: Instrument, spot: float, pcr: float | None, today: date, picked=None) -> str | None:
    if picked is None:
        picked, why = await _straddle(ctx, underlying, today, spot)
        if picked is None:
            return why
    _expiry, strike, ce, ce_price, pe, pe_price = picked
    await ctx.open_leg(ce, "sell", _qty(ce), ce_price)
    await ctx.open_leg(pe, "sell", _qty(pe), pe_price)
    state.update({
        "regime": NEUTRAL, "opened_at": ctx.now.isoformat(), "reference_spot": spot, "straddle_strike": strike,
        "legs": {"ce": _leg(ce, "sell", _qty(ce), ce_price, ctx.now), "pe": _leg(pe, "sell", _qty(pe), pe_price, ctx.now)},
    })
    if pcr is not None:
        state["pcr_at_entry"] = pcr
    return None


async def _roll_option(ctx, state: dict, underlying: Instrument, spot: float, today: date) -> str:
    leg = state["legs"]["option"]
    old_expiry = date.fromisoformat(leg["expiry"])
    expiry = await _option_expiry(ctx, underlying, today, skip_today=True, after=old_expiry)
    if expiry is None:
        return "option roll due: next week's NIFTY options aren't listed"
    picked, why = await _option_near_premium(ctx, underlying, leg["option_type"], expiry, spot)
    if picked is None:
        return f"option roll due: {why}"
    price = await _exit_price(ctx, leg, spot, today)
    if price is None:
        return f"option roll due: no live price for {leg['symbol']}"
    new, premium = picked
    await _close_legs(ctx, [leg], [price], "option_rollover")
    await ctx.open_leg(new, "sell", _qty(new), premium)
    state["legs"]["option"] = _leg(new, "sell", _qty(new), premium, ctx.now)
    return f"rolled {leg['symbol']} @ {price:.2f} -> {new.symbol} @ {premium:.2f}"


async def _roll_future(ctx, state: dict, underlying: Instrument, spot: float, today: date) -> str:
    leg = state["legs"]["future"]
    new = await _future(ctx, underlying, today, after=date.fromisoformat(leg["expiry"]))
    if new is None:
        return "futures roll due: next month's NIFTY future isn't listed"
    new_price = await ctx.get_price(new.id)
    price = await _exit_price(ctx, leg, spot, today)
    if new_price is None or price is None:
        return f"futures roll due: no live price for {new.symbol if new_price is None else leg['symbol']}"
    await _close_legs(ctx, [leg], [price], "futures_rollover")
    await ctx.open_leg(new, leg["side"], _qty(new), new_price)
    state["legs"]["future"] = _leg(new, leg["side"], _qty(new), new_price, ctx.now)
    return f"rolled {leg['symbol']} @ {price:.2f} -> {new.symbol} @ {new_price:.2f}"


# --------------------------------------------------------------- the PCR --

async def _signal_pcr(ctx, mark: datetime, spot: float) -> tuple[float | None, str, bool]:
    """(pcr, detail, record_found) for the 15-minute close `mark`."""
    snap = (
        await ctx.db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == PCR_UNDERLYING, PcrSnapshot.ts == mark))
    ).scalar_one_or_none()
    if snap is None or not snap.expiries:
        return None, f"waiting for the {mark.astimezone(IST):%H:%M} PCR record", False
    expiry = date.fromisoformat(snap.expiries[0])
    rows = (
        await ctx.db.execute(
            select(PcrStrikeOi.strike, PcrStrikeOi.option_type, PcrStrikeOi.oi).where(
                PcrStrikeOi.snapshot_id == snap.id, PcrStrikeOi.expiry == expiry,
            )
        )
    ).all()
    pcr, detail = total_oi_pcr([(float(k), t, oi) for k, t, oi in rows], snap.spot or spot)
    return pcr, f"{detail}, {expiry:%d %b}", True


# ------------------------------------------------------------------ cycle --

def _label(state: dict) -> str:
    legs = state.get("legs") or {}
    if state.get("regime") == NEUTRAL:
        return f"NEUTRAL straddle {state.get('straddle_strike'):.0f} (ref {state.get('reference_spot'):.1f})"
    if state.get("regime") in (BULLISH, BEARISH):
        return f"{state['regime']}: {legs['future']['symbol']} + short {legs['option']['symbol']}"
    return "FLAT"


def _pcr_text(state: dict) -> str:
    pcr, at = state.get("last_pcr"), state.get("last_pcr_at")
    return f"PCR {pcr:.3f} @ {at}" if pcr is not None else "no PCR yet"


async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    today = now_ist.date()
    state = ctx.state
    state.setdefault("regime", FLAT)
    state.setdefault("legs", {})
    underlying = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))).scalar_one_or_none()
    if underlying is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return
    force_exit = state.pop("force_exit", False)
    spot = await ctx.get_price(underlying.id)

    if force_exit:
        if spot is None:
            state["force_exit"] = True
            ctx.note("hold", reason="exit asked: no live NIFTY price yet -- next cycle")
            return
        why = await _flatten(ctx, state, spot, today, "manual")
        if why:
            state["force_exit"] = True
            ctx.note("hold", reason=f"exit asked: {why} -- next cycle")
        else:
            ctx.note("exited", signal="EXIT", reason=state.get("last_exit") or "flat")
        return

    if not nse_market_open(ctx.now):
        ctx.note("hold", reason=f"market closed | {_label(state)} | {_pcr_text(state)}")
        return
    if spot is None:
        ctx.note("skipped", reason=f"no live NIFTY price | {_label(state)}")
        return
    regime = state["regime"]

    # 15:10: the neutral straddle closes, and no new neutral entry today.
    if regime == NEUTRAL and now_ist.time() >= STRADDLE_FORCE_EXIT:
        state["neutral_locked_date"] = today.isoformat()
        why = await _flatten(ctx, state, spot, today, "neutral_1510_close")
        if why:
            ctx.note("hold", reason=f"15:10 close due: {why} -- next cycle")
        else:
            ctx.note("exited", signal="EXIT", reason=f"{state['last_exit']} | neutral locked for today")
        return
    if regime == NEUTRAL:
        ctx.wake_at(datetime.combine(today, STRADDLE_FORCE_EXIT, tzinfo=IST))

    # Neutral recenter: spot STRADDLE_SHIFT_POINTS from the reference.
    if regime == NEUTRAL and abs(spot - state["reference_spot"]) >= STRADDLE_SHIFT_POINTS:
        picked, why = await _straddle(ctx, underlying, today, spot)
        if picked is None:
            ctx.note("hold", reason=f"recenter due (spot {spot:.1f}, ref {state['reference_spot']:.1f}): {why}")
            return
        old_ref, old_strike = state["reference_spot"], state["straddle_strike"]
        why = await _flatten(ctx, state, spot, today, "neutral_recenter")
        if why:
            ctx.note("hold", reason=f"recenter due: {why} -- next cycle")
            return
        await _enter_neutral(ctx, state, underlying, spot, None, today, picked)
        ctx.note("rolled", signal="STRADDLE_SHIFT",
                 reason=f"spot {spot:.1f} moved {spot - old_ref:+.1f} from {old_ref:.1f}: straddle {old_strike:.0f} -> {state['straddle_strike']:.0f}")
        return

    notes: list[str] = []

    # A leg already past expiry (stopped over its expiry day): roll it now.
    if regime in (BULLISH, BEARISH):
        if _expired(state["legs"]["option"], today):
            notes.append(await _roll_option(ctx, state, underlying, spot, today))
        if _expired(state["legs"]["future"], today):
            notes.append(await _roll_future(ctx, state, underlying, spot, today))

    # The completed 15-minute signal -- only a close after this run started:
    # a run switched on at 10:52 (paper, or live from its card) waits for
    # 11:00 rather than act at once on the 10:45 close it never saw.
    mark = signal_mark(ctx.now)
    started = getattr(ctx, "started_at", None)
    if mark is not None and started is not None and mark <= started:
        notes.append(f"started {started.astimezone(IST):%H:%M}: first signal at the next close")
        mark = None
    signal = None
    if mark is not None and state.get("last_signal_mark") != mark.isoformat():
        pcr, detail, found = await _signal_pcr(ctx, mark, spot)
        at = f"{mark.astimezone(IST):%H:%M}"
        if pcr is None:
            if found:  # a record that can't be trusted: this close is skipped
                state["last_signal_mark"] = mark.isoformat()
            notes.append(f"{at} signal: {detail}")
        else:
            state.update({"last_pcr": pcr, "last_pcr_at": at})
            done, signal, text = await _on_signal(ctx, state, underlying, spot, pcr, now_ist, at)
            if done:
                state["last_signal_mark"] = mark.isoformat()
            notes.append(f"{at} TOTAL_OI_PCR {pcr:.3f} ({detail}): {text}")
            if done and state["regime"] in (BULLISH, BEARISH):
                notes += await _signal_rolls(ctx, state, underlying, spot, pcr, today, mark)

    # The agreed 15:00 roll on the option's (and the future's) expiry day,
    # once the 15:00 close has been acted on -- or by 15:05 without it.
    if state["regime"] in (BULLISH, BEARISH):
        roll_by = datetime.combine(today, EXPIRY_ROLL_BY, tzinfo=IST)
        last = state.get("last_signal_mark")
        handled_1500 = last is not None and datetime.fromisoformat(last) >= roll_by
        if handled_1500 or now_ist >= roll_by + LATE_RECORD_GRACE:
            if state["legs"]["option"]["expiry"] == today.isoformat():
                notes.append(await _roll_option(ctx, state, underlying, spot, today))
            if state["legs"]["future"]["expiry"] == today.isoformat():
                notes.append(await _roll_future(ctx, state, underlying, spot, today))

    next_close = _next_signal_time(now_ist)
    if next_close is not None:
        ctx.wake_at(next_close + timedelta(seconds=5))
    summary = f"{_label(state)} | {_pcr_text(state)}"
    rolled = any(n.startswith("rolled") for n in notes)
    action = {"ENTER": "entered", "EXIT": "exited"}.get((signal or "").split("_")[0], "rolled" if rolled else "hold")
    ctx.note(action, signal=signal or ("ROLL" if rolled else None), reason=" | ".join(notes + [summary]))


def _next_signal_time(now_ist: datetime) -> datetime | None:
    t = now_ist.replace(second=0, microsecond=0)
    t += timedelta(minutes=15 - t.minute % 15)
    if t.time() < FIRST_SIGNAL_TIME:
        t = datetime.combine(now_ist.date(), FIRST_SIGNAL_TIME, tzinfo=IST)
    return t if t.time() <= LAST_SIGNAL_TIME else None


async def _on_signal(ctx, state: dict, underlying: Instrument, spot: float, pcr: float, now_ist: datetime, at: str):
    """One completed-close decision: (done, signal, text). Not done (a leg
    without a price) is tried again next cycle with the same close's PCR."""
    today = now_ist.date()
    regime = state["regime"]
    if regime != FLAT:
        if not exit_due(regime, pcr):
            return True, None, f"hold {regime}"
        why = await _flatten(ctx, state, spot, today, f"pcr_{pcr:.3f}_{at}")
        if why:
            return False, None, f"{regime} exit due: {why}"
        return True, "EXIT", state["last_exit"]

    target = entry_regime(pcr)
    if target is None:
        return True, None, "flat (transition band)"
    if target == NEUTRAL:
        if state.get("neutral_locked_date") == today.isoformat():
            return True, None, "neutral entry blocked: 15:10 lock is active"
        if now_ist.time() >= STRADDLE_FORCE_EXIT:  # it would close at once (the 15:15 close)
            return True, None, "neutral entries end at 15:10"
        why = await _enter_neutral(ctx, state, underlying, spot, pcr, today)
        if why:
            return False, None, f"neutral entry: {why}"
        legs = state["legs"]
        return True, "ENTER_NEUTRAL", (f"sold straddle {legs['ce']['symbol']} @ {legs['ce']['entry_price']:.2f} + "
                                       f"{legs['pe']['symbol']} @ {legs['pe']['entry_price']:.2f}, ref spot {spot:.1f}")
    why = await _enter_directional(ctx, state, underlying, target, spot, pcr, today)
    if why:
        return False, None, f"{target.lower()} entry: {why}"
    legs = state["legs"]
    return True, "ENTER_BULL" if target == BULLISH else "ENTER_BEAR", (
        f"{'long' if target == BULLISH else 'short'} {legs['future']['symbol']} @ {legs['future']['entry_price']:.2f}, "
        f"sold {legs['option']['symbol']} @ {legs['option']['entry_price']:.2f}"
    )


async def _signal_rolls(ctx, state: dict, underlying: Instrument, spot: float, pcr: float, today: date, mark: datetime) -> list[str]:
    """The file's rolls at a signal: the option on its expiry day while PCR
    is past the entry level; the future on its expiry day (still held, so
    still valid)."""
    notes = []
    if state["legs"]["option"]["expiry"] == today.isoformat() and past_entry_level(state["regime"], pcr):
        notes.append(await _roll_option(ctx, state, underlying, spot, today))
    if state["legs"]["future"]["expiry"] == today.isoformat():
        notes.append(await _roll_future(ctx, state, underlying, spot, today))
    return notes
