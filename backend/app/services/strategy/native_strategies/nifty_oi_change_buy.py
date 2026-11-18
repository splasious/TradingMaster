"""
OI Change Buy 15 MIN -- one NIFTY option bought on the last 15 minutes' change in OI (native)
============================================================================================

Agreed 16 Nov (version 2, the same day: the last 15 minutes' change, not
the day's). At every 15-minute close from FIRST_MARK (09:30) to LAST_MARK
(15:15), the PCR Analysis tab's record for that close (models/pcr.py,
services/options/pcr_snapshots.py: NIFTY, its 4 expiries, ATM +-40
strikes) gives the change in OI over that 15-minute candle -- since the
record before, that close's row's Put dOI and Call dOI:

  Put dOI (15 min) higher than Call dOI (15 min)   buy a CALL
  Call dOI (15 min) higher than Put dOI (15 min)   buy a PUT
  equal                                           no change

The two are compared as they are, a fall in OI (unwinding) a negative
change: call unwinding with puts building, a call; put unwinding with
calls building, a put; both unwinding, the side unwinding less is the
higher -- calls covered harder, a call; puts covered harder, a put.

What it buys: LOTS (1) lot of the ATM option -- the STRIKE_STEP (50-point)
strike nearest NIFTY at that moment -- on the nearest weekly expiry
(next week's on that weekly's own expiry day), at the ask.

At each later close: the same side wanted, the option held is kept (the
same contract, wherever NIFTY has gone since); the other side wanted, the
held option is sold at the bid and, FLIP_GAP (5 seconds) later, the other
side's ATM option is bought at the ask. Nothing held and a side wanted,
it's bought at once.

The first minute (version 4, 18 Nov): before the first close, at
FIRST_READ (09:15:50) the live change in OI since the 09:15 record -- the
PCR Analysis tab's LIVE row (services/options/pcr_live.py), so the first
minute's build-up -- picks the side the same way, and its ATM option is
bought at once; not had by FIRST_BUY_BY (09:16:00) -- no reading, no ask --
nothing is bought until the 09:30 close, which then reads its 15 minutes as
any close. Live only (a backtest has no live reading).

The record lands a few seconds after its close; it's looked for every
RECHECK (1 second) until it does, for up to RECORD_WAIT (2 minutes) -- a
close whose record isn't in by then is passed over, what's held kept. A
card started between two closes begins at the next one.

At DAY_CLOSE (15:29) whatever is held is sold at the bid (a position left
from an earlier day, the app down at 15:29, at the first check after) and
nothing more is bought that day. A buy that can't be had -- no ask yet --
is tried again every second until it is, or until 15:29.

The trailing stop (version 3, 17 Nov; +15% / 5% picked on the backtest of
the 36 stored sessions, scripts/oi_trail_backtest.py): the held option's
bid, read every RECHECK (1 second), switches the trail on once it reaches
TRAIL_START (15%) above the price paid; from then the stop sits TRAIL (5%)
below the highest bid since, only ever moving up -- at least +9.25% over
cost the moment it starts. The bid at or under the stop: sold at the bid
("trailing_stop"), and nothing more until the next 15-minute close, which
buys whatever side it reads -- the same one again included. No take-profit
or stop-loss besides; a loser still runs to the flip or 15:29. In a backtest there is no order book (ctx.is_backtest): the
last price stands for both bid and ask.

Paper only, no Telegram alerts. Runs through
services/paper_trading/native_runner.py.
"""

import uuid
from datetime import datetime, time as dtime, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.pcr import PcrSnapshot
from app.services.broker.zerodha_broker import IST

UNDERLYING_SYMBOL = "NIFTY 50"
PCR_UNDERLYING = "NIFTY"  # the PCR records' name for it
REGIME = "oi change buy"
STRIKE_STEP = 50
LOTS = 1
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
FIRST_MARK = dtime(9, 30)
LAST_MARK = dtime(15, 15)
MARK_STEP = timedelta(minutes=15)
DAY_CLOSE = dtime(15, 29)
FLIP_GAP = timedelta(seconds=5)
RECORD_WAIT = timedelta(minutes=2)
FIRST_READ = dtime(9, 15, 50)  # version 4: the first minute's live change in OI read then
FIRST_BUY_BY = dtime(9, 16)  # and its option bought by then, or not at all (the 09:30 close decides)
RECHECK = timedelta(seconds=1)
TRAIL_START = 0.15  # version 3: the trail switches on once the bid is this far above the price paid
TRAIL = 0.05  # then the stop sits this far below the highest bid since, only moving up
SIDE_NAME = {"CE": "call", "PE": "put"}


def atm_strike(spot: float) -> float:
    """The STRIKE_STEP strike nearest NIFTY (a half-way NIFTY rounds up)."""
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def wanted_side(put_change: float | None, call_change: float | None) -> str | None:
    """"CE" when the put dOI is the higher, "PE" when the call's is, None when equal or unknown."""
    if put_change is None or call_change is None or put_change == call_change:
        return None
    return "CE" if put_change > call_change else "PE"


def latest_mark(now_ist: datetime) -> datetime | None:
    """The latest 15-minute close from FIRST_MARK to LAST_MARK that has come, today (IST); None before 09:30."""
    first = datetime.combine(now_ist.date(), FIRST_MARK, tzinfo=IST)
    if now_ist < first:
        return None
    steps = min(int((now_ist - first) / MARK_STEP), int((datetime.combine(now_ist.date(), LAST_MARK, tzinfo=IST) - first) / MARK_STEP))
    return first + steps * MARK_STEP


def next_mark(now_ist: datetime) -> datetime | None:
    """The next 15-minute close from FIRST_MARK to LAST_MARK after `now_ist`, today; None after 15:15."""
    first = datetime.combine(now_ist.date(), FIRST_MARK, tzinfo=IST)
    last = datetime.combine(now_ist.date(), LAST_MARK, tzinfo=IST)
    if now_ist < first:
        return first
    mark = first + (int((now_ist - first) / MARK_STEP) + 1) * MARK_STEP
    return mark if mark <= last else None


def leg_pnl(leg: dict, exit_price: float) -> float:
    return (exit_price - leg["entry_price"]) * leg["quantity"]


async def first_minute_reading(now_utc: datetime) -> tuple[dict | None, str | None]:
    """(the PCR Analysis tab's live row -- every record field against the latest
    record, the 09:15 one now -- None) or (None, why there isn't one)."""
    from app.services.options.pcr_live import live_row

    return await live_row(PCR_UNDERLYING, now_utc)


async def bid_ask(ctx, instrument_id: uuid.UUID) -> tuple[float, float] | None:
    getter = None if getattr(ctx, "is_backtest", False) else getattr(ctx, "get_bid_ask", None)
    if getter is None:  # a backtest: no order book, the last price both ways
        price = await ctx.get_price(instrument_id)
        return (price, price) if price is not None else None
    return await getter(instrument_id)


async def evaluate(ctx) -> None:
    now_utc = ctx.now
    now_ist = now_utc.astimezone(IST)
    today = now_ist.date()
    day_close = datetime.combine(today, DAY_CLOSE, tzinfo=IST)

    underlying = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))
    ).scalar_one_or_none()
    if underlying is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return

    state = ctx.state
    force_exit = state.pop("force_exit", False)
    position = state.get("position")

    async def sell_price(leg: dict) -> float:
        """What selling the held option fills at now: the bid, else the last
        price, else (an expired contract nothing prices) what it cost."""
        iid = uuid.UUID(leg["instrument_id"])
        quote = await bid_ask(ctx, iid)
        if quote is not None:
            return quote[0]
        last = await ctx.get_price(iid)
        return last if last is not None else leg["entry_price"]

    async def close(exit_reason: str) -> tuple[float, float]:
        """Sells the held option; (its price, the P&L before charges)."""
        leg = position["legs"]["long"]
        price = await sell_price(leg)
        instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        await ctx.close_leg(instrument, "sell", leg["quantity"], price)
        pnl = leg_pnl(leg, price)
        cost = leg["entry_price"] * leg["quantity"]
        await ctx.record_trade(
            legs=[{"instrument_id": leg["instrument_id"], "side": "long", "quantity": leg["quantity"],
                   "entry_price": leg["entry_price"], "exit_price": price}],
            pnl=pnl, pnl_pct=(pnl / cost * 100) if cost else 0.0, exit_reason=exit_reason,
            opened_at=datetime.fromisoformat(position["opened_at"]), closed_at=now_utc,
        )
        state["position"] = None
        return price, pnl

    def held_text() -> str:
        text = f"{position['option']} bought at {position['legs']['long']['entry_price']:,.2f}"
        if position.get("trail_stop") is not None:
            return f"{text}; trailing stop {position['trail_stop']:,.2f} (5% under the highest bid {position['peak_bid']:,.2f})"
        if position.get("trail_starts_at") is not None:
            return f"{text}; trail starts at a bid of {position['trail_starts_at']:,.2f} (+{TRAIL_START * 100:g}%)"
        return text

    first_read = datetime.combine(today, FIRST_READ, tzinfo=IST)
    first_buy_by = datetime.combine(today, FIRST_BUY_BY, tzinfo=IST)
    first_due = state.get("first_minute") != today.isoformat() and now_ist < first_buy_by and not getattr(ctx, "is_backtest", False)

    def next_text() -> str:
        if first_due and position is None:
            return f"at {FIRST_READ:%H:%M:%S} (the first minute's change in OI)"
        return _next_text(now_ist)

    def wake_for_next() -> None:
        """Run again at the next 15-minute close (09:15:50 first, its day), or at 15:29 while something is held."""
        mark = next_mark(now_ist)
        if mark is not None:
            ctx.wake_at(mark)
        if first_due and now_ist < first_read:
            ctx.wake_at(first_read)
        if state.get("position") and now_ist < day_close:
            ctx.wake_at(day_close)
            ctx.wake_at(now_utc + RECHECK)  # version 3: the trail read every second

    # -- a manual exit, and 15:29 (or a position left from an earlier day)
    if position is not None and force_exit:
        price, pnl = await close("manual")
        state.pop("pending", None)
        ctx.note("exited", signal="SELL", reason=f"manual exit: sold at {price:,.2f}; P&L {pnl:+,.0f} before charges")
        wake_for_next()
        return
    opened_day = datetime.fromisoformat(position["opened_at"]).astimezone(IST).date() if position else None
    if position is not None and (now_ist >= day_close or opened_day < today):
        option = position["option"]
        price, pnl = await close("time_cutoff_329pm")
        state.pop("pending", None)
        ctx.note("exited", signal="SELL", reason=f"15:29 close: {option} sold at {price:,.2f}; P&L {pnl:+,.0f} before charges")
        return
    if now_ist >= day_close:
        state.pop("pending", None)
        ctx.note("skipped", reason=f"day done: nothing bought after {DAY_CLOSE:%H:%M}; next decision at {FIRST_MARK:%H:%M}")
        return

    # -- the trailing stop (version 3): on the bid, every second while something is held
    if position is not None:
        leg = position["legs"]["long"]
        quote = await bid_ask(ctx, uuid.UUID(leg["instrument_id"]))
        if quote is not None:
            bid_now = quote[0]
            position["trail_starts_at"] = position.get("trail_starts_at") or round(leg["entry_price"] * (1 + TRAIL_START), 2)
            position["peak_bid"] = round(max(position.get("peak_bid") or bid_now, bid_now), 2)
            if position.get("trail_stop") is not None or position["peak_bid"] >= position["trail_starts_at"]:
                position["trail_stop"] = round(max(position.get("trail_stop") or 0.0, position["peak_bid"] * (1 - TRAIL)), 2)
            if position.get("trail_stop") is not None and bid_now <= position["trail_stop"]:
                option, peak, stop = position["option"], position["peak_bid"], position["trail_stop"]
                price, pnl = await close("trailing_stop")
                position = None
                state.pop("pending", None)
                current = latest_mark(now_ist)
                if current is not None:
                    state["last_mark"] = current.isoformat()  # flat until the next close, whatever this one reads
                ctx.note("exited", signal="SELL", reason=(
                    f"trailing stop: {option} sold at {price:,.2f} (the bid), its stop {stop:,.2f} -- {TRAIL * 100:g}% under the "
                    f"highest bid {peak:,.2f}; P&L {pnl:+,.0f} before charges; next decision {next_text()}"
                ))
                wake_for_next()
                return

    # -- the 15-minute close: that candle's change in OI decides the side
    mark = latest_mark(now_ist)
    decided = ""
    if mark is not None and "last_mark" not in state and now_ist - mark >= RECORD_WAIT:
        state["last_mark"] = mark.isoformat()  # a card started between closes begins at the next one
    if mark is not None and state.get("last_mark") != mark.isoformat():
        mark_utc = mark.astimezone(timezone.utc)
        snap = (
            await ctx.db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == PCR_UNDERLYING, PcrSnapshot.ts == mark_utc))
        ).scalar_one_or_none()
        if snap is None:
            if now_ist - mark < RECORD_WAIT:
                ctx.note("hold" if position else "skipped", reason=f"waiting for the {mark:%H:%M} change-in-OI record")
                ctx.wake_at(now_utc + RECHECK)
                return
            state["last_mark"] = mark.isoformat()
            decided = f"no {mark:%H:%M} change-in-OI record in {RECORD_WAIT.seconds // 60} minutes: passed over; "
        else:
            state["last_mark"] = mark.isoformat()
            put_15m, call_15m = snap.put_oi_change, snap.call_oi_change  # since the record before (version 2)
            state["last_oi"] = {"at": f"{mark:%H:%M}", "put_change_15m": put_15m, "call_change_15m": call_15m}
            side = wanted_side(put_15m, call_15m)
            reading = (f"{mark:%H:%M} change in OI over the last 15 minutes: put {put_15m:+,.0f}, call {call_15m:+,.0f}"
                       if put_15m is not None and call_15m is not None else f"{mark:%H:%M}: no 15-minute change in OI on the record")
            if position is not None:
                for old_key in ("put_change_day", "call_change_day"):  # version 1's reading, on a position carried over
                    position.pop(old_key, None)
                position["put_change_15m"], position["call_change_15m"], position["oi_read_at"] = put_15m, call_15m, f"{mark:%H:%M}"
            if side is None:
                decided = f"{reading} -- no change; "
            elif position is not None and position["option_type"] == side:
                decided = f"{reading} -- still a {SIDE_NAME[side]}: held; "
            elif position is not None:
                old = position["option"]
                price, pnl = await close("oi_flip")
                position = None
                due = now_utc + FLIP_GAP
                state["pending"] = {"option_type": side, "due": due.isoformat(), "mark": f"{mark:%H:%M}", "put_change_15m": put_15m,
                                    "call_change_15m": call_15m}
                ctx.note("exited", signal="SELL", reason=(
                    f"{reading} -- now a {SIDE_NAME[side]}: {old} sold at {price:,.2f}, P&L {pnl:+,.0f} before charges; "
                    f"the {SIDE_NAME[side]} bought at {due.astimezone(IST):%H:%M:%S}"
                ))
                ctx.wake_at(due)
                return
            else:
                state["pending"] = {"option_type": side, "due": now_utc.isoformat(), "mark": f"{mark:%H:%M}", "put_change_15m": put_15m,
                                    "call_change_15m": call_15m}
                decided = f"{reading} -- a {SIDE_NAME[side]}; "

    # -- the first minute (version 4): at 09:15:50 the live change in OI since the 09:15 record
    if first_due and position is None and state.get("pending") is None and first_read <= now_ist:
        row, why = await first_minute_reading(now_utc)
        if row is None:
            ctx.note("skipped", reason=f"first minute: no live change in OI yet ({why}); tried again until {FIRST_BUY_BY:%H:%M:%S}")
            ctx.wake_at(now_utc + RECHECK)
            return
        state["first_minute"] = today.isoformat()
        put_1m, call_1m = row.get("put_oi_change"), row.get("call_oi_change")
        state["last_oi"] = {"at": "first minute", "put_change_15m": put_1m, "call_change_15m": call_1m}
        side = wanted_side(put_1m, call_1m)
        if side is None:
            decided = "first minute: no side in the change in OI; "
        else:
            state["pending"] = {"option_type": side, "due": now_utc.isoformat(), "mark": "09:15 first minute",
                                "put_change_15m": put_1m, "call_change_15m": call_1m, "buy_by": first_buy_by.isoformat()}
            decided = (f"first minute's change in OI (since the {as_ist_hhmm(row.get('prev_ts'))} record): put {put_1m:+,.0f}, "
                       f"call {call_1m:+,.0f} -- a {SIDE_NAME[side]}; ")

    # -- a buy due: at once, or 5 seconds after a flip's sale
    pending = state.get("pending")
    if pending is not None and pending.get("buy_by") and now_utc >= datetime.fromisoformat(pending["buy_by"]):
        state.pop("pending", None)  # the first minute's buy not had by 09:16: the 09:30 close decides
        pending = None
        decided += f"the first minute's buy wasn't had by {FIRST_BUY_BY:%H:%M} -- dropped; "
    if pending is not None and position is None:
        due = datetime.fromisoformat(pending["due"])
        if now_utc < due:
            ctx.note("skipped", reason=f"{decided}the {SIDE_NAME[pending['option_type']]} is bought at {due.astimezone(IST):%H:%M:%S}")
            ctx.wake_at(due)
            return
        spot = await ctx.get_price(underlying.id)
        expiries = await ctx.list_weekly_expiries(underlying.id, today)
        weekly = (expiries[1] if expiries and today == expiries[0] and len(expiries) > 1 else expiries[0]) if expiries else None
        why = None
        if spot is None:
            why = "no live NIFTY price yet"
        elif weekly is None or weekly == today:
            why = "no NIFTY weekly after today listed"
        else:
            strike, option_type = atm_strike(spot), pending["option_type"]
            instrument = await ctx.find_option(underlying.id, weekly, strike, option_type)
            quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
            if instrument is None:
                why = f"no {weekly:%d %b} {strike:,.0f} {option_type} in the catalog"
            elif quote is None:
                why = f"no bid/ask for the {weekly:%d %b} {strike:,.0f} {option_type} yet"
        if why is not None:
            ctx.note("skipped", reason=f"{decided}buying the {SIDE_NAME[pending['option_type']]} waits: {why}")
            ctx.wake_at(now_utc + RECHECK)
            return
        price = quote[1]
        quantity = float(LOTS * (instrument.lot_size or DEFAULT_LOT_SIZE))
        await ctx.open_leg(instrument, "buy", quantity, price)
        option = f"{weekly:%d %b} {strike:,.0f} {option_type}"
        state["position"] = position = {
            "regime": REGIME,
            "bias": f"long {SIDE_NAME[option_type]}",
            "option": option,
            "option_type": option_type,
            "strike": strike,
            "weekly_expiry": weekly.isoformat(),
            "entry_spot": spot,
            "decided_at": pending.get("mark"),
            "put_change_15m": pending.get("put_change_15m"),
            "call_change_15m": pending.get("call_change_15m"),
            "trail_starts_at": round(price * (1 + TRAIL_START), 2),
            "peak_bid": None,
            "trail_stop": None,
            "legs": {"long": {"instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": "buy",
                              "expiry": weekly.isoformat(), "quantity": quantity, "entry_price": price}},
            "opened_at": now_utc.isoformat(),
        }
        state.pop("pending", None)
        ctx.note("entered", signal="BUY", reason=(
            f"{decided}bought {LOTS} lot of {option} at {price:,.2f} (the ask), NIFTY {spot:,.1f}; trail from a bid of "
            f"{price * (1 + TRAIL_START):,.2f} (+{TRAIL_START * 100:g}%), {TRAIL * 100:g}% under its peak; sold at 15:29 or when the side flips"
        ))
        wake_for_next()
        return

    if position is not None:
        ctx.note("hold", reason=f"{decided}{held_text()}; next decision {next_text()}; sold at {DAY_CLOSE:%H:%M}")
    else:
        ctx.note("skipped", reason=f"{decided}nothing held; next decision {next_text()}")
    wake_for_next()


def as_ist_hhmm(ts) -> str:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return as_aware_utc(ts).astimezone(IST).strftime("%H:%M") if isinstance(ts, datetime) else "latest"


def _next_text(now_ist: datetime) -> str:
    mark = next_mark(now_ist)
    return f"at the {mark:%H:%M} close" if mark is not None else "tomorrow at 09:30"
