"""
OHLC4 Buy 15 MIN -- one NIFTY option bought on which side of the previous 15-minute candle's (O+H+L+C)/4 NIFTY runs (native)
=============================================================================================================================

Agreed 27 Nov, as OI Change Buy 15 MIN (nifty_oi_change_buy.py) but with
one condition in place of the change in OI: the previous 15-minute NIFTY
candle's typical price, LEVEL = (open + high + low + close) / 4. From the
SESSION_OPEN (09:15) open to DAY_CLOSE (15:29), read every RECHECK (1
second) against live NIFTY:

  NIFTY above LEVEL     hold a CALL   -- its stop the previous candle's low
  NIFTY below LEVEL     hold a PUT    -- its stop the previous candle's high
  NIFTY at LEVEL        no change

The previous candle is the last finished 15-minute NIFTY candle (09:15-
09:30, ... 15:15-15:30); from 09:15 to 09:30, the session before's last
(15:15-15:30). A candle not yet in when it should be -- its end passed,
the candle sync behind -- means no decision until it is: what's held kept.

Always on one side (agreed 27 Nov: a flip at once): holding the call and
NIFTY under LEVEL, the call is sold at the bid at once and, FLIP_GAP (5
seconds) later, the ATM put bought at the ask -- the same the other way.
The stop is part of it: a call is never held under LEVEL, and the previous
candle's low is always under its LEVEL, so NIFTY reaches the stop only
through a flip (a gap past it included); such a sale is recorded as
"stop_loss", any other as "level_flip". Nothing held and a side wanted,
it's bought at once. The side when the buy falls due is the one bought.

What it buys: LOTS (1) lot of the ATM option -- the STRIKE_STEP (50-point)
strike nearest NIFTY at that moment -- on the nearest weekly expiry (next
week's on that weekly's own expiry day), at the ask. No trailing stop (as
agreed), no take-profit.

Held overnight, as OI Change Buy version 7: nothing sold at 15:29 -- the
option held then is carried, and read again from the next 09:15 open
against the session before's last candle. On the held option's own expiry
day at ROLL_AT (12:00) it's sold and, FLIP_GAP later, the ATM option on
next week's weekly bought -- on the side NIFTY is on then, as any buy (the
same side, unless NIFTY crossed the level in those 5 seconds); one found
expired is rolled at the first check, one still held at 15:29 on its
expiry day sold then. Its
trades are charged no brokerage (BROKERAGE_PER_ORDER 0, as Kotak's
intraday); STT, exchange, SEBI, stamp duty and GST as always.

Paper only, no Telegram alerts. In a backtest there is no order book
(ctx.is_backtest): the last price stands for both bid and ask. Runs through
services/paper_trading/native_runner.py.
"""

import uuid
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.services.backfill_platform.coverage import is_trading_day, next_trading_day, previous_trading_day
from app.services.broker.zerodha_broker import IST

UNDERLYING_SYMBOL = "NIFTY 50"
REGIME = "ohlc4 buy"
TIMEFRAME = "15m"
BAR = timedelta(minutes=15)
STRIKE_STEP = 50
LOTS = 1
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
SESSION_OPEN = dtime(9, 15)
FIRST_CLOSE = dtime(9, 30)  # the session's first candle ends
LAST_CLOSE = dtime(15, 30)  # its last
DAY_CLOSE = dtime(15, 29)  # nothing bought after it; what's held carried overnight
ROLL_AT = dtime(12, 0)  # on the held option's expiry day, rolled to next week's weekly then
FLIP_GAP = timedelta(seconds=5)
RECHECK = timedelta(seconds=1)
BROKERAGE_PER_ORDER = 0.0  # no brokerage, as Kotak's intraday; the taxes and fees as always (trade_record)
SIDE_NAME = {"CE": "call", "PE": "put"}


def atm_strike(spot: float) -> float:
    """The STRIKE_STEP strike nearest NIFTY (a half-way NIFTY rounds up)."""
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def level_of(bar: dict) -> float:
    """A candle's typical price: (open + high + low + close) / 4."""
    return (float(bar["open"]) + float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 4


def wanted_side(spot: float, level: float) -> str | None:
    """"CE" above the level, "PE" under it, None at it."""
    if spot > level:
        return "CE"
    return "PE" if spot < level else None


def previous_end(now_ist: datetime) -> datetime:
    """When the previous candle -- the last that should have finished by `now_ist` -- ended (IST): the latest
    15-minute close 09:30-15:30 today, or before 09:30 the session before's 15:30."""
    today = now_ist.date()
    first = datetime.combine(today, FIRST_CLOSE, tzinfo=IST)
    if now_ist < first or not is_trading_day(today):
        return datetime.combine(previous_trading_day(today), LAST_CLOSE, tzinfo=IST)
    steps = int((now_ist - first) / BAR)
    return min(first + steps * BAR, datetime.combine(today, LAST_CLOSE, tzinfo=IST))


def leg_pnl(leg: dict, exit_price: float) -> float:
    return (exit_price - leg["entry_price"]) * leg["quantity"]


def next_session_open(now_ist: datetime) -> datetime:
    """09:15 (IST) of the next NSE session -- today's, before its open."""
    today = now_ist.date()
    if is_trading_day(today) and now_ist.time() < SESSION_OPEN:
        return datetime.combine(today, SESSION_OPEN, tzinfo=IST)
    return datetime.combine(next_trading_day(today), SESSION_OPEN, tzinfo=IST)


async def previous_candle(ctx, nifty: Instrument, now_ist: datetime) -> tuple[dict | None, str]:
    """(the previous finished 15-minute NIFTY candle, "") or (None, why not)."""
    expected = previous_end(now_ist)
    bars = await ctx.get_candles(nifty.id, TIMEFRAME, 3)
    if not bars:
        return None, "no NIFTY 15-minute candles yet"
    newest = bars[-1]
    ended = (as_aware_utc(newest["ts"]) + BAR).astimezone(IST)
    if ended < expected:
        return None, f"waiting for the NIFTY candle to {expected:%H:%M} (newest ends {ended:%d %b %H:%M})"
    return newest, ""


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
    open_at = datetime.combine(today, SESSION_OPEN, tzinfo=IST)
    day_close = datetime.combine(today, DAY_CLOSE, tzinfo=IST)
    roll_at = datetime.combine(today, ROLL_AT, tzinfo=IST)

    nifty = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))).scalar_one_or_none()
    if nifty is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return

    state = ctx.state
    state["brokerage_per_order"] = BROKERAGE_PER_ORDER  # what its trades are charged (paper_trading/native_runner.py)
    force_exit = state.pop("force_exit", False)
    position = state.get("position")
    trading = is_trading_day(today)

    async def sell_price(leg: dict) -> float:
        """What selling the held option fills at now: the bid, else the last price, else what it cost."""
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
        return text

    def wake() -> None:
        if trading and open_at <= now_ist < day_close:
            ctx.wake_at(now_utc + RECHECK)  # live NIFTY against the level, every second
        elif trading and now_ist < open_at:
            ctx.wake_at(open_at)
        if state.get("position") and now_ist < day_close:
            ctx.wake_at(day_close)

    # -- a manual exit
    if position is not None and force_exit:
        price, pnl = await close("manual")
        state.pop("pending", None)
        ctx.note("exited", signal="SELL", reason=f"manual exit: sold at {price:,.2f}; P&L {pnl:+,.0f} before charges")
        wake()
        return
    # -- from 15:29: nothing bought; what's held carried overnight -- unless it expires today
    expiry = date.fromisoformat(position["weekly_expiry"]) if position is not None else None
    if now_ist >= day_close or not trading:
        state.pop("pending", None)
        next_open = next_session_open(now_ist)
        if position is not None and trading and expiry <= today:
            option = position["option"]
            price, pnl = await close("expiry_day_close")
            ctx.note("exited", signal="SELL", reason=(
                f"15:29 on its expiry day, not rolled at {ROLL_AT:%H:%M}: {option} sold at {price:,.2f}; P&L {pnl:+,.0f} before "
                f"charges; next decision {next_open:%a %d %b} at the {SESSION_OPEN:%H:%M} open"
            ))
            return
        if position is not None:
            ctx.note("hold", reason=f"carried overnight: {held_text()}; next decision at the {SESSION_OPEN:%H:%M} open {next_open:%a %d %b}")
            ctx.wake_at(next_open)
            return
        ctx.note("skipped", reason=f"day done: nothing bought after {DAY_CLOSE:%H:%M}; next decision {next_open:%a %d %b} at the "
                                   f"{SESSION_OPEN:%H:%M} open")
        return
    if now_ist < open_at:
        ctx.note("hold" if position else "skipped", reason=(
            (f"{held_text()}; " if position else "") + f"next decision at the {SESSION_OPEN:%H:%M} open"))
        wake()
        return

    # -- the rollover: on the held option's expiry day at 12:00 (or found expired), to next week's, same side
    if position is not None and (expiry < today or (expiry == today and now_ist >= roll_at)):
        option, side = position["option"], position["option_type"]
        price, pnl = await close("expiry_rollover")
        position = None
        due = now_utc + FLIP_GAP
        state["pending"] = {"option_type": side, "due": due.isoformat(), "why": f"{ROLL_AT:%H:%M} rollover"}
        ctx.note("exited", signal="SELL", reason=(
            f"expiry rollover: {option} sold at {price:,.2f} (the bid), P&L {pnl:+,.0f} before charges; the ATM option on next "
            f"week's weekly bought at {due.astimezone(IST):%H:%M:%S} on the side NIFTY is on then"
        ))
        ctx.wake_at(due)
        return

    # -- the rule: live NIFTY against the previous candle's (O+H+L+C)/4
    bar, why = await previous_candle(ctx, nifty, now_ist)
    spot = await ctx.get_price(nifty.id)
    if bar is None or spot is None:
        why = why or "no live NIFTY price yet"
        ctx.note("hold" if position else "skipped", reason=(f"{held_text()} kept; " if position else "") + why)
        wake()
        return
    level = round(level_of(bar), 2)
    low, high = float(bar["low"]), float(bar["high"])
    start = as_aware_utc(bar["ts"]).astimezone(IST)
    label = f"the {start:%H:%M} candle" if start.date() == today else f"the {start:%d %b %H:%M} candle"
    side = wanted_side(spot, level)
    reading = f"NIFTY {spot:,.2f} against {label}'s (O+H+L+C)/4 {level:,.2f} (low {low:,.2f}, high {high:,.2f})"
    state["last_level"] = {"candle": start.isoformat(), "level": level, "low": low, "high": high, "spot": spot,
                           "at": now_ist.strftime("%H:%M:%S")}
    if position is not None:
        position["level"], position["candle"] = level, start.isoformat()
        position["stop"] = low if position["option_type"] == "CE" else high
        if side is not None and side != position["option_type"]:
            held_side, option = position["option_type"], position["option"]
            stopped = spot <= low if held_side == "CE" else spot >= high
            price, pnl = await close("stop_loss" if stopped else "level_flip")
            position = None
            due = now_utc + FLIP_GAP
            state["pending"] = {"option_type": side, "due": due.isoformat(), "why": reading}
            crossed = (f"past its {'low' if held_side == 'CE' else 'high'}: the stop" if stopped else f"now a {SIDE_NAME[side]}")
            ctx.note("exited", signal="SELL", reason=(
                f"{reading} -- {crossed}: {option} sold at {price:,.2f}, P&L {pnl:+,.0f} before charges; "
                f"the {SIDE_NAME[side]} bought at {due.astimezone(IST):%H:%M:%S}"
            ))
            ctx.wake_at(due)
            return

    pending = state.get("pending")
    if position is None and pending is not None and side is not None and pending["option_type"] != side:
        pending.update(option_type=side, why=reading)  # the side NIFTY is on when it's bought, a rollover's too
    elif position is None and pending is None and side is not None:
        state["pending"] = pending = {"option_type": side, "due": now_utc.isoformat(), "why": reading}

    # -- a buy due: at once, or 5 seconds after a flip's sale
    if position is None and pending is not None:
        due = datetime.fromisoformat(pending["due"])
        if now_utc < due:
            ctx.note("skipped", reason=f"{reading}; the {SIDE_NAME[pending['option_type']]} is bought at {due.astimezone(IST):%H:%M:%S}")
            ctx.wake_at(due)
            return
        expiries = await ctx.list_weekly_expiries(nifty.id, today)
        weekly = (expiries[1] if expiries and today == expiries[0] and len(expiries) > 1 else expiries[0]) if expiries else None
        option_type, strike = pending["option_type"], atm_strike(spot)
        instrument = await ctx.find_option(nifty.id, weekly, strike, option_type) if weekly and weekly != today else None
        quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
        if quote is None:
            wait = ("no NIFTY weekly after today listed" if not weekly or weekly == today
                    else f"no {weekly:%d %b} {strike:,.0f} {option_type} in the catalog" if instrument is None
                    else f"no bid/ask for the {weekly:%d %b} {strike:,.0f} {option_type} yet")
            ctx.note("skipped", reason=f"{reading}; buying the {SIDE_NAME[option_type]} waits: {wait}")
            ctx.wake_at(now_utc + RECHECK)
            return
        price = quote[1]
        quantity = float(LOTS * (instrument.lot_size or DEFAULT_LOT_SIZE))
        await ctx.open_leg(instrument, "buy", quantity, price)
        option = f"{weekly:%d %b} {strike:,.0f} {option_type}"
        state["position"] = position = {
            "regime": REGIME, "bias": f"long {SIDE_NAME[option_type]}", "option": option, "option_type": option_type,
            "strike": strike, "weekly_expiry": weekly.isoformat(), "entry_spot": spot, "decided_by": pending.get("why"),
            "level": level, "candle": start.isoformat(), "stop": low if option_type == "CE" else high,
            "legs": {"long": {"instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": "buy",
                              "expiry": weekly.isoformat(), "quantity": quantity, "entry_price": price}},
            "opened_at": now_utc.isoformat(),
        }
        state.pop("pending", None)
        ctx.note("entered", signal="BUY", reason=(
            f"{reading} -- a {SIDE_NAME[option_type]}: bought {LOTS} lot of {option} at {price:,.2f} (the ask); its stop NIFTY "
            f"{'under' if option_type == 'CE' else 'over'} {position['stop']:,.2f} (the previous candle's {'low' if option_type == 'CE' else 'high'}), "
            f"moving with each new candle; flipped when NIFTY crosses the level; held overnight, rolled at {ROLL_AT:%H:%M} on its expiry day"
        ))
        wake()
        return

    if position is not None:
        ctx.note("hold", reason=(f"{reading} -- still a {SIDE_NAME[position['option_type']]}: {held_text()}; stop NIFTY "
                                 f"{'under' if position['option_type'] == 'CE' else 'over'} {position['stop']:,.2f}"))
    else:
        ctx.note("skipped", reason=f"{reading} -- at the level: nothing bought yet")
    wake()
