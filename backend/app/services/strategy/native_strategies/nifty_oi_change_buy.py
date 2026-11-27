"""
OI Change Buy 15 MIN -- one NIFTY option bought on the live 15-minute candle's change in OI, held overnight (native)
===================================================================================================================

Version 10 (27 Nov): two changes, as agreed the same day.
  1. Each 15-minute candle's first minute (WAIT_AFTER_MARK: 09:15-09:16,
     09:30-09:31 ... 15:15-15:16) decides nothing: the live change in OI
     is read from 1 minute in, and no buy is made in it -- a buy due then
     waits for the minute to pass (the trailing stop still works). The
     09:00 row's decision is so made at 09:16.
  2. The forming 15-minute candle's colour -- live NIFTY against its open,
     the NIFTY of its own 15-minute record -- must agree with the side
     bought: a PE isn't bought while the candle is green (NIFTY above its
     open), a CE isn't while it's red; at its open (or no record) either
     is. The OI flip still sells the held option (agreed: sell it, stay
     flat); the buy waits, read every second, and is made once the colour
     agrees with the side the OI then says. Every buy, the 12:00 roll's
     too.

Version 9 (27 Nov): checking starts at the SESSION_OPEN (09:15) open, not
the 09:30 close. At the open the side comes from the OPEN_RECORD (09:00)
row's own change in OI -- the overnight change, Put dOI against Call dOI,
as below -- once a day: a carried option on the other side sold and the
other side bought FLIP_GAP later, nothing held bought. From then on the
live candle decides, counted from the 09:15 record and each later one (a
reading still against the 09:00 record waits for the 09:15 one). No 09:00
record: the live candle from 09:15 straight away.

Version 8 (26 Nov): the side follows the LIVE candle -- the one forming
now -- instead of being picked at each 15-minute close; and its trades are
charged no brokerage (BROKERAGE_PER_ORDER 0, as Kotak's intraday: STT,
exchange, SEBI, stamp duty and GST as always). From the 09:30 close until
DAY_CLOSE (15:29), every few seconds (the PCR Analysis tab's LIVE row,
services/options/pcr_live.py, one Kite reading every 15 seconds shared by
every reader): that candle's Put dOI and Call dOI so far -- since the
latest 15-minute record -- decide as below, the moment the higher one
changes; no average (AVERAGE_CANDLES 1: a candle is its own). Nothing read
before the 09:30 record is in (the pre-open and 09:15-09:30 candles), and
no live reading (Zerodha not logged in, Kite down) means what's held is
kept. In a backtest, with no live reading, each candle decides at its
close. The rest -- the ATM weekly bought at the ask, a flip sold at the bid
and the other side bought FLIP_GAP later, the trail, flat after a trailing
stop until the next 15-minute close, held overnight, the 12:00 expiry-day
roll -- as version 7, below.

Before version 8:

Agreed 16 Nov (version 2, the same day: the last 15 minutes' change, not
the day's); version 6, 24 Nov: the average of the last AVERAGE_CANDLES (2)
fifteen-minute candles, picked on the backtest of 40 stored sessions
(scripts/oi_avg_optimise.py: 1-12 candles, with and without the trail).
At every 15-minute close from FIRST_MARK (09:30) to LAST_MARK (15:15), the
PCR Analysis tab's records (models/pcr.py, services/options/pcr_snapshots.py:
NIFTY, its 4 expiries, ATM +-40 strikes) give each candle's change in OI --
since the record before, a row's Put dOI and Call dOI. The candles are the
records closing a trading candle, CANDLES_FROM (09:30) to CANDLES_TO
(15:30): that close's and the one before -- at 09:30 the previous session's
last (15:15-15:30) candle, the overnight 09:00 and pre-open 09:15 records
left out. Averaged over those candles:

  Put dOI higher than Call dOI     buy a CALL
  Call dOI higher than Put dOI     buy a PUT
  equal                            no change

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
it's bought at once. The first decision of the day is the 09:30 close
(version 6: the 09:15:50 first-minute buy of version 4 is gone).

The record lands a few seconds after its close; it's looked for every
RECHECK (1 second) until it does, for up to 2 minutes -- a
close whose record isn't in by then is passed over, what's held kept. A
card started between two closes begins at the next one.

Held overnight (version 7, 25 Nov; backtested in scripts/oi_overnight_backtest.py):
nothing is sold at DAY_CLOSE (15:29) -- whatever is held then is carried to
the next session, and nothing more is bought that day. From the next
session's SESSION_OPEN (09:15) the trail reads its bid every second again
-- a gap open under the stop sells at once -- and the 09:30 close keeps it
or flips it as any close. The rollover: on the held option's own expiry
day, at ROLL_AT (12:00), it is sold at the bid ("expiry_rollover") and,
FLIP_GAP later, the same side's ATM option on next week's weekly is bought
at the ask -- unless a flip or the trail has sold it first. One found
expired (the app down on its expiry day) is rolled at the first check; one
still held at 15:29 on its expiry day is sold then, not carried. A buy that
can't be had -- no ask yet -- is tried again every second until it is, or
until 15:29.

The trailing stop (version 3, 17 Nov; +15% / 5% picked on the backtest of
the 36 stored sessions, scripts/oi_trail_backtest.py, and kept in version
6): the held option's bid, read every RECHECK (1 second), switches the
trail on once it reaches TRAIL_START (15%) above the price paid; from then
the stop sits TRAIL (5%) below the highest bid since, only ever moving up
-- at least +9.25% over cost the moment it starts. The bid at or under the
stop: sold at the bid ("trailing_stop"), and nothing more until the next
15-minute close, which buys whatever side it reads -- the same one again
included. No stop-loss before the trail (version 6: version 5's Rs 1,200 a
lot is gone) and no take-profit. In a backtest there is no order book
(ctx.is_backtest): the last price stands for both bid and ask.

Paper only, no Telegram alerts. Runs through
services/paper_trading/native_runner.py.
"""

import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.pcr import PcrSnapshot
from app.services.backfill_platform.coverage import is_trading_day, next_trading_day
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
DAY_CLOSE = dtime(15, 29)  # version 7: no sale then -- nothing bought after it, what's held carried overnight
SESSION_OPEN = dtime(9, 15)  # version 7: the trail on a carried option from the next session's open; version 9: checking from it
OPEN_RECORD = dtime(9, 0)  # version 9: this record's own change in OI (the overnight) decides the side at the open
ROLL_AT = dtime(12, 0)  # version 7: on the held option's expiry day, rolled to next week's weekly then
FLIP_GAP = timedelta(seconds=5)
WAIT_AFTER_MARK = timedelta(minutes=1)  # version 10: each 15-minute candle's first minute decides nothing, buys nothing
CANDLE = timedelta(minutes=15)  # version 10: the candles from 09:15
AVERAGE_CANDLES = 1  # version 8: a candle is its own (2 in versions 6-7: the side from the average over this many candles)
LIVE_CHECK = timedelta(seconds=15)  # version 8: how often the live candle's reading is new (pcr_live.LIVE_TTL)
BROKERAGE_PER_ORDER = 0.0  # version 8: no brokerage, as Kotak's intraday; the taxes and fees as always (trade_record)
CANDLES_FROM, CANDLES_TO = dtime(9, 30), dtime(15, 30)  # the records closing a trading candle
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


def candle_start(now_ist: datetime) -> datetime | None:
    """Version 10: the 15-minute candle `now_ist` falls in (from 09:15, IST); None before 09:15."""
    first = datetime.combine(now_ist.date(), SESSION_OPEN, tzinfo=IST)
    if now_ist < first:
        return None
    return first + int((now_ist - first) / CANDLE) * CANDLE


def first_minute_ends(now_ist: datetime) -> datetime | None:
    """Version 10: when the first minute of the candle `now_ist` is in ends, while it's in it; None otherwise."""
    start = candle_start(now_ist)
    if start is None or now_ist >= start + WAIT_AFTER_MARK:
        return None
    return start + WAIT_AFTER_MARK


def candle_colour(price: float | None, open_price: float | None) -> str | None:
    """Version 10: "green" above the candle's open, "red" under it, None at it or unknown."""
    if price is None or open_price is None or price == open_price:
        return None
    return "green" if price > open_price else "red"


def colour_blocks(option_type: str, colour: str | None) -> bool:
    """Version 10: a PE isn't bought on a green candle, a CE isn't on a red one."""
    return (option_type == "PE" and colour == "green") or (option_type == "CE" and colour == "red")


async def candle_open(db, start: datetime) -> float | None:
    """Version 10: the candle's open -- the NIFTY of its own 15-minute record (taken a few seconds after the mark)."""
    snap = (await db.execute(
        select(PcrSnapshot.spot).where(PcrSnapshot.underlying == PCR_UNDERLYING, PcrSnapshot.ts == start.astimezone(timezone.utc))
    )).scalars().first()
    return float(snap) if snap is not None else None


def leg_pnl(leg: dict, exit_price: float) -> float:
    return (exit_price - leg["entry_price"]) * leg["quantity"]


def next_session_open(now_ist: datetime) -> datetime:
    """09:15 (IST) of the next NSE session -- today's, before its open."""
    today = now_ist.date()
    if is_trading_day(today) and now_ist.time() < SESSION_OPEN:
        return datetime.combine(today, SESSION_OPEN, tzinfo=IST)
    return datetime.combine(next_trading_day(today), SESSION_OPEN, tzinfo=IST)


def closes_a_candle(ts: datetime) -> bool:
    """A record at a close from 09:30 to 15:30 (IST): one 15-minute trading candle's change in OI."""
    return CANDLES_FROM <= as_aware_utc(ts).astimezone(IST).time() <= CANDLES_TO


async def candle_average(db, mark_utc: datetime) -> tuple[float, float, list[datetime]] | None:
    """(average Put dOI, average Call dOI, the candles' closes in IST, oldest first) over the last
    AVERAGE_CANDLES records closing a trading candle at or before `mark_utc` -- the session before
    filling in at 09:30 -- or None without one."""
    rows = (
        await db.execute(
            select(PcrSnapshot).where(
                PcrSnapshot.underlying == PCR_UNDERLYING, PcrSnapshot.ts <= mark_utc,
                PcrSnapshot.put_oi_change.is_not(None), PcrSnapshot.call_oi_change.is_not(None),
            ).order_by(PcrSnapshot.ts.desc()).limit(AVERAGE_CANDLES + 4)  # the overnight 09:00 and 09:15 records in between
        )
    ).scalars().all()
    candles = [snap for snap in rows if closes_a_candle(snap.ts)][:AVERAGE_CANDLES]
    if not candles:
        return None
    put = sum(snap.put_oi_change for snap in candles) / len(candles)
    call = sum(snap.call_oi_change for snap in candles) / len(candles)
    return put, call, [as_aware_utc(snap.ts).astimezone(IST) for snap in reversed(candles)]


async def live_row(now_utc: datetime) -> tuple[dict | None, str | None]:
    """The PCR Analysis tab's LIVE row now: (the row, None), or (None, why there isn't one)."""
    # imported here: it reaches for Kite only when run live
    from app.services.options.pcr_live import live_row as read

    return await read(PCR_UNDERLYING, now_utc)


async def open_change(db, today: date) -> dict | None:
    """Version 9: the OPEN_RECORD (09:00) row's own Put and Call dOI -- the overnight change -- today, or None."""
    at = datetime.combine(today, OPEN_RECORD, tzinfo=IST).astimezone(timezone.utc)
    snap = (
        await db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == PCR_UNDERLYING, PcrSnapshot.ts == at))
    ).scalar_one_or_none()
    if snap is None or snap.put_oi_change is None or snap.call_oi_change is None:
        return None
    return {"put": float(snap.put_oi_change), "call": float(snap.call_oi_change), "label": f"the {OPEN_RECORD:%H:%M} row (overnight)",
            "since": at.astimezone(IST).isoformat()}


async def read_change(ctx, now_utc: datetime) -> tuple[dict | None, str]:
    """Version 8: ({"put", "call", "label", "since"}, "") -- the live candle's Put and Call dOI so far,
    since the latest 15-minute record (version 9: the 09:15 one on) -- or (None, why not). In a
    backtest, the latest closed candle."""
    now_ist = now_utc.astimezone(IST)
    first = datetime.combine(now_ist.date(), SESSION_OPEN, tzinfo=IST)  # version 9: the 09:15 record on (09:30 before)
    if getattr(ctx, "is_backtest", False):
        average = await candle_average(ctx.db, now_utc)
        if average is None or average[2][-1] < datetime.combine(now_ist.date(), FIRST_MARK, tzinfo=IST):
            return None, f"no {FIRST_MARK:%H:%M} change-in-OI record yet"
        return {"put": average[0], "call": average[1], "label": f"the {average[2][-1]:%H:%M} candle",
                "since": average[2][-1].isoformat()}, ""
    row, why = await live_row(now_utc)
    if row is None:
        return None, why or "no live reading"
    put, call, prev = row.get("put_oi_change"), row.get("call_oi_change"), row.get("prev_ts")
    if put is None or call is None or prev is None:
        return None, "the live reading has no change in OI yet"
    since = as_aware_utc(prev if isinstance(prev, datetime) else datetime.fromisoformat(str(prev))).astimezone(IST)
    if since < first:
        return None, f"waiting for the {SESSION_OPEN:%H:%M} record: the live candle starts there"
    read_at = as_aware_utc(row["ts"] if isinstance(row.get("ts"), datetime) else now_utc).astimezone(IST)
    return {"put": float(put), "call": float(call), "label": f"live {read_at:%H:%M:%S} (since {since:%H:%M})",
            "since": since.isoformat()}, ""


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
    roll_at = datetime.combine(today, ROLL_AT, tzinfo=IST)

    underlying = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))
    ).scalar_one_or_none()
    if underlying is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return

    state = ctx.state
    state["brokerage_per_order"] = BROKERAGE_PER_ORDER  # version 8: what its trades are charged (paper_trading/native_runner.py)
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
        opened = datetime.fromisoformat(position["opened_at"]).astimezone(IST)
        if opened.date() < today:
            text += f" on {opened:%d %b}"
        if position["weekly_expiry"] == today.isoformat() and now_ist < roll_at:
            text += f"; expires today: rolled to next week's at {ROLL_AT:%H:%M}"
        if position.get("trail_stop") is not None:
            return f"{text}; trailing stop {position['trail_stop']:,.2f} (5% under the highest bid {position['peak_bid']:,.2f})"
        if position.get("trail_starts_at") is not None:
            return f"{text}; trail starts at a bid of {position['trail_starts_at']:,.2f} (+{TRAIL_START * 100:g}%)"
        return text

    def next_text() -> str:
        return _next_text(now_ist)

    def wake_for_next() -> None:
        """Run again at the next 15-minute close, every LIVE_CHECK while the live candle decides, and every
        second until 15:29 while something is held."""
        mark = next_mark(now_ist)
        if mark is not None:
            ctx.wake_at(mark)
        open_at = datetime.combine(today, SESSION_OPEN, tzinfo=IST)
        quiet = first_minute_ends(now_ist)
        if quiet is not None and now_ist < day_close and is_trading_day(today):
            ctx.wake_at(quiet)  # version 10: decisions from 1 minute into the candle
        elif open_at <= now_ist < day_close:
            ctx.wake_at(now_utc + LIVE_CHECK)  # version 8: the live candle
        elif now_ist < open_at and is_trading_day(today):
            ctx.wake_at(open_at)  # version 9: the open's decision
        if state.get("position") and now_ist < day_close:
            ctx.wake_at(day_close)
            ctx.wake_at(now_utc + RECHECK)  # version 3: the trail read every second

    # -- a manual exit
    if position is not None and force_exit:
        price, pnl = await close("manual")
        state.pop("pending", None)
        ctx.note("exited", signal="SELL", reason=f"manual exit: sold at {price:,.2f}; P&L {pnl:+,.0f} before charges")
        wake_for_next()
        return
    # -- from 15:29: nothing bought; what's held carried overnight (version 7) -- unless it expires today
    expiry = date.fromisoformat(position["weekly_expiry"]) if position is not None else None
    if now_ist >= day_close:
        state.pop("pending", None)
        next_open = next_session_open(now_ist)
        if position is not None and expiry <= today:
            option = position["option"]
            price, pnl = await close("expiry_day_close")
            ctx.note("exited", signal="SELL", reason=(
                f"15:29 on its expiry day, not rolled at {ROLL_AT:%H:%M}: {option} sold at {price:,.2f}; P&L {pnl:+,.0f} before "
                f"charges; next decision {next_open:%a %d %b} at the {SESSION_OPEN:%H:%M} open"
            ))
            return
        if position is not None:
            ctx.note("hold", reason=(
                f"carried overnight: {held_text()}; the trail from the {SESSION_OPEN:%H:%M} open {next_open:%a %d %b}, the next "
                f"decision at {first_decision():%H:%M} (the {OPEN_RECORD:%H:%M} row's change in OI, then the live candle)"
            ))
            ctx.wake_at(next_open)
            return
        ctx.note("skipped", reason=(f"day done: nothing bought after {DAY_CLOSE:%H:%M}; next decision {next_open:%a %d %b} at the "
                                    f"{SESSION_OPEN:%H:%M} open"))
        return

    # -- the trailing stop (version 3): on the bid, every second while held
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
                until = next_mark(now_ist) or day_close  # flat until the next close, whatever the live candle reads
                state["flat_until"] = until.astimezone(timezone.utc).isoformat()
                ctx.note("exited", signal="SELL", reason=(
                    f"trailing stop: {option} sold at {price:,.2f} (the bid), its stop {stop:,.2f} -- {TRAIL * 100:g}% under the "
                    f"highest bid {peak:,.2f}; P&L {pnl:+,.0f} before charges; next decision {next_text()}"
                ))
                wake_for_next()
                return

    # -- the rollover (version 7): on the held option's expiry day at 12:00 (or found expired), to next week's, same side
    if position is not None and (expiry < today or (expiry == today and now_ist >= roll_at)):
        option, side = position["option"], position["option_type"]
        price, pnl = await close("expiry_rollover")
        position = None
        due = now_utc + FLIP_GAP
        state["pending"] = {"option_type": side, "due": due.isoformat(), "mark": f"{ROLL_AT:%H:%M} rollover"}
        ctx.note("exited", signal="SELL", reason=(
            f"expiry rollover: {option} sold at {price:,.2f} (the bid), P&L {pnl:+,.0f} before charges; the same side's ATM "
            f"{SIDE_NAME[side]} on next week's weekly bought at {due.astimezone(IST):%H:%M:%S}"
        ))
        ctx.wake_at(due)
        return

    # -- version 8: the live candle decides the side, to 15:29 -- version 9: from the 09:15 open, the 09:00 row first
    decided = ""
    live_now = datetime.combine(today, SESSION_OPEN, tzinfo=IST) <= now_ist < day_close
    quiet_until = first_minute_ends(now_ist) if live_now else None
    if quiet_until is not None:  # version 10: each candle's first minute decides nothing
        live_now = False
        decided = (f"the first minute of the {candle_start(now_ist):%H:%M} candle: no decision until "
                   f"{quiet_until:%H:%M}; ")
    flat_until = state.get("flat_until")
    if flat_until and now_utc < datetime.fromisoformat(flat_until):
        live_now = False
        decided = f"flat after the trailing stop until the {datetime.fromisoformat(flat_until).astimezone(IST):%H:%M} close; "
    elif flat_until:
        state.pop("flat_until", None)
    for old_key in ("last_mark",):  # version 7's: the close it last read
        state.pop(old_key, None)
    if live_now:
        reading = None
        if state.get("opened_on") != today.isoformat():  # version 9: once a day, at the open -- the 09:00 row decides
            state["opened_on"] = today.isoformat()
            reading = await open_change(ctx.db, today)
        why = ""
        if reading is None:
            reading, why = await read_change(ctx, now_utc)
        if reading is None:
            decided = f"no live change-in-OI reading ({why}): what's held kept; "
        else:
            put, call, label = reading["put"], reading["call"], reading["label"]
            state["last_oi"] = {"at": label, "put_change": put, "call_change": call, "since": reading["since"]}
            side = wanted_side(put, call)
            text = f"{label} change in OI: put {put:+,.0f}, call {call:+,.0f}"
            if position is not None:
                for old_key in ("put_change_day", "call_change_day", "stop_loss_at", "put_change_15m", "call_change_15m",
                                "put_avg", "call_avg"):  # earlier versions' fields, on a position carried over
                    position.pop(old_key, None)
                position["put_change_live"], position["call_change_live"], position["oi_read_at"] = put, call, label
            pending = state.get("pending")
            if side is None:
                decided = f"{text} -- no change; "
            elif pending is not None and position is None:
                if pending["option_type"] != side:  # the side when it's bought -- version 10: the roll's too
                    pending.update(option_type=side, put_change=put, call_change=call, mark=label)
                decided = f"{text} -- a {SIDE_NAME[side]}; "
            elif position is not None and position["option_type"] == side:
                decided = f"{text} -- still a {SIDE_NAME[side]}: held; "
            elif position is not None:
                old = position["option"]
                price, pnl = await close("oi_flip")
                position = None
                due = now_utc + FLIP_GAP
                state["pending"] = {"option_type": side, "due": due.isoformat(), "mark": label, "put_change": put, "call_change": call}
                ctx.note("exited", signal="SELL", reason=(
                    f"{text} -- now a {SIDE_NAME[side]}: {old} sold at {price:,.2f}, P&L {pnl:+,.0f} before charges; "
                    f"the {SIDE_NAME[side]} bought at {due.astimezone(IST):%H:%M:%S}"
                ))
                ctx.wake_at(due)
                return
            else:
                state["pending"] = {"option_type": side, "due": now_utc.isoformat(), "mark": label, "put_change": put, "call_change": call}
                decided = f"{text} -- a {SIDE_NAME[side]}; "

    # -- a buy due: at once, or 5 seconds after a flip's sale
    pending = state.get("pending")
    if pending is not None and position is None:
        due = datetime.fromisoformat(pending["due"])
        if now_utc < due:
            ctx.note("skipped", reason=f"{decided}the {SIDE_NAME[pending['option_type']]} is bought at {due.astimezone(IST):%H:%M:%S}")
            ctx.wake_at(due)
            return
        side_name = SIDE_NAME[pending["option_type"]]
        if quiet_until is not None:  # version 10: no buy in a candle's first minute
            ctx.note("skipped", reason=f"{decided}the {side_name} is bought from {quiet_until:%H:%M}, if the candle's colour agrees")
            ctx.wake_at(quiet_until)
            return
        spot = await ctx.get_price(underlying.id)
        start = candle_start(now_ist)
        open_price = await candle_open(ctx.db, start) if start is not None else None
        colour = candle_colour(spot, open_price)
        state["candle_colour"] = {"candle": start.isoformat() if start else None, "open": open_price, "nifty": spot,
                                  "colour": colour, "at": now_ist.strftime("%H:%M:%S")}
        if colour_blocks(pending["option_type"], colour):  # version 10: flat until the colour agrees
            ctx.note("skipped", reason=(
                f"{decided}the {start:%H:%M} candle is {colour} (NIFTY {spot:,.2f} against its open {open_price:,.2f}): the "
                f"{side_name} isn't bought; flat, checked every second until the colour agrees with the OI's side"
            ))
            ctx.wake_at(now_utc + RECHECK)
            return
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
            "put_change_live": pending.get("put_change"),
            "call_change_live": pending.get("call_change"),
            "trail_starts_at": round(price * (1 + TRAIL_START), 2),
            "peak_bid": None,
            "trail_stop": None,
            "legs": {"long": {"instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": "buy",
                              "expiry": weekly.isoformat(), "quantity": quantity, "entry_price": price}},
            "opened_at": now_utc.isoformat(),
        }
        state.pop("pending", None)
        ctx.note("entered", signal="BUY", reason=(
            f"{decided}bought {LOTS} lot of {option} at {price:,.2f} (the ask), NIFTY {spot:,.1f}; the trail starts at a bid of "
            f"{price * (1 + TRAIL_START):,.2f} (+{TRAIL_START * 100:g}%), then {TRAIL * 100:g}% under its peak; no stop-loss; "
            f"held overnight; sold when the live candle's side flips, rolled at {ROLL_AT:%H:%M} on its expiry day"
        ))
        wake_for_next()
        return

    if position is not None:
        ctx.note("hold", reason=f"{decided}{held_text()}; next decision {next_text()}; held overnight")
    else:
        ctx.note("skipped", reason=f"{decided}nothing held; next decision {next_text()}")
    wake_for_next()


def first_decision() -> dtime:
    """Version 10: the session's first decision -- a minute into the 09:15 candle."""
    return (datetime.combine(date(2000, 1, 1), SESSION_OPEN) + WAIT_AFTER_MARK).time()


def _next_text(now_ist: datetime) -> str:
    first = datetime.combine(now_ist.date(), SESSION_OPEN, tzinfo=IST)
    if now_ist < first and is_trading_day(now_ist.date()):
        return f"at {first_decision():%H:%M}, a minute into the {SESSION_OPEN:%H:%M} candle (the {OPEN_RECORD:%H:%M} row's change in OI), then on the live candle"
    if first <= now_ist < datetime.combine(now_ist.date(), DAY_CLOSE, tzinfo=IST):
        return (f"on the live candle until {DAY_CLOSE:%H:%M}, from 1 minute into each 15-minute candle; a buy only when the "
                f"candle's colour agrees")
    return (f"{next_trading_day(now_ist.date()):%a %d %b} at {first_decision():%H:%M} (the {OPEN_RECORD:%H:%M} row's change "
            f"in OI), then on the live candle")
