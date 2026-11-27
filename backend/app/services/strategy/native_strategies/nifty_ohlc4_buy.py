"""
OHLC4 Buy 5 MIN -- one NIFTY option bought on which side of the previous 5-minute candle's (O+H+L+C)/4 NIFTY closes (native)
============================================================================================================================

Agreed 27 Nov, as OI Change Buy 15 MIN (nifty_oi_change_buy.py) but with
one condition in place of the change in OI, on NIFTY's 5-minute candles
(agreed the same day, after the backtest: decided at each 5-minute close).
At each 5-minute close from FIRST_CLOSE (09:20, the 09:15 candle's) to
LAST_DECISION (15:25, the 15:20 candle's), the candle just closed against
the one before it, LEVEL = that candle's (open + high + low + close) / 4:

  closed above LEVEL    hold a CALL   -- its stop the candle before's low
  closed below LEVEL    hold a PUT    -- its stop the candle before's high
  closed at LEVEL       no change

The 09:15 candle is measured against the session before's last (15:25-
15:30). A candle is decided once it is in -- the candle sync stores it 10-
70 s after its close; until then, what's held kept. Between closes nothing
is decided: what's held is held.

Always on one side (agreed 27 Nov: a flip at once): holding the call and a
candle closing under LEVEL, the call is sold at the bid and, FLIP_GAP (5
seconds) later, the ATM put bought at the ask -- the same the other way.
The stop is part of it: the candle before's low is always under its LEVEL,
so a close past the stop is also a close past LEVEL; such a sale is
recorded as "stop_loss", any other as "level_flip". Nothing held and a side
decided, it's bought at once.

What it buys: LOTS (1) lot of the ATM option -- the STRIKE_STEP (50-point)
strike nearest NIFTY at that moment -- on the nearest weekly expiry (next
week's on that weekly's own expiry day), at the ask. No trailing stop (as
agreed), no take-profit.

Held overnight, as OI Change Buy version 7: nothing sold at 15:29 -- the
option held then is carried to the next session's 09:20 close. On the held
option's own expiry day at ROLL_AT (12:00) it's sold and, FLIP_GAP later,
the same side's ATM option on next week's weekly bought; one found expired
is rolled at the first check, one still held at 15:29 on its expiry day
sold then. Its trades are charged no brokerage (BROKERAGE_PER_ORDER 0, as
Kotak's intraday); STT, exchange, SEBI, stamp duty and GST as always.

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
TIMEFRAME = "5m"
BAR = timedelta(minutes=5)
STRIKE_STEP = 50
LOTS = 1
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
SESSION_OPEN = dtime(9, 15)
FIRST_CLOSE = dtime(9, 20)  # the session's first candle closes: the first decision
LAST_DECISION = dtime(15, 25)  # the last close decided (15:20-15:25); the 15:30 one comes after DAY_CLOSE
LAST_CLOSE = dtime(15, 30)  # the session's last candle closes
DAY_CLOSE = dtime(15, 29)  # nothing bought after it; what's held carried overnight
ROLL_AT = dtime(12, 0)  # on the held option's expiry day, rolled to next week's weekly then
FLIP_GAP = timedelta(seconds=5)
RECHECK = timedelta(seconds=1)  # while a closed candle isn't in yet
BROKERAGE_PER_ORDER = 0.0  # no brokerage, as Kotak's intraday; the taxes and fees as always (trade_record)
SIDE_NAME = {"CE": "call", "PE": "put"}


def atm_strike(spot: float) -> float:
    """The STRIKE_STEP strike nearest NIFTY (a half-way NIFTY rounds up)."""
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def level_of(bar: dict) -> float:
    """A candle's typical price: (open + high + low + close) / 4."""
    return (float(bar["open"]) + float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 4


def wanted_side(close: float, level: float) -> str | None:
    """"CE" closed above the level, "PE" under it, None at it."""
    if close > level:
        return "CE"
    return "PE" if close < level else None


def latest_close(now_ist: datetime) -> datetime:
    """The latest 5-minute close (IST) by `now_ist`: today's 09:20-15:30, or before 09:20 the session before's 15:30."""
    today = now_ist.date()
    first = datetime.combine(today, FIRST_CLOSE, tzinfo=IST)
    if now_ist < first or not is_trading_day(today):
        return datetime.combine(previous_trading_day(today), LAST_CLOSE, tzinfo=IST)
    steps = int((now_ist - first) / BAR)
    return min(first + steps * BAR, datetime.combine(today, LAST_CLOSE, tzinfo=IST))


def next_decision(now_ist: datetime) -> datetime:
    """The next 5-minute close decided after `now_ist`: 09:20-15:25, else the next session's 09:20."""
    today = now_ist.date()
    if is_trading_day(today):
        first = datetime.combine(today, FIRST_CLOSE, tzinfo=IST)
        if now_ist < first:
            return first
        following = latest_close(now_ist) + BAR
        if following <= datetime.combine(today, LAST_DECISION, tzinfo=IST):
            return following
    return datetime.combine(next_trading_day(today), FIRST_CLOSE, tzinfo=IST)


def leg_pnl(leg: dict, exit_price: float) -> float:
    return (exit_price - leg["entry_price"]) * leg["quantity"]


async def closed_candles(ctx, nifty: Instrument, now_ist: datetime) -> tuple[dict | None, dict | None, str]:
    """(the 5-minute NIFTY candle closed last, the one before it, "") or (None, None, why not yet)."""
    expected = latest_close(now_ist)
    bars = await ctx.get_candles(nifty.id, TIMEFRAME, 3)
    if len(bars) < 2:
        return None, None, "no NIFTY 5-minute candles yet"
    newest = bars[-1]
    ended = (as_aware_utc(newest["ts"]) + BAR).astimezone(IST)
    if ended < expected:
        return None, None, f"waiting for the NIFTY candle to {expected:%H:%M} (newest ends {ended:%d %b %H:%M})"
    return newest, bars[-2], ""


async def bid_ask(ctx, instrument_id: uuid.UUID) -> tuple[float, float] | None:
    getter = None if getattr(ctx, "is_backtest", False) else getattr(ctx, "get_bid_ask", None)
    if getter is None:  # a backtest: no order book, the last price both ways
        price = await ctx.get_price(instrument_id)
        return (price, price) if price is not None else None
    return await getter(instrument_id)


def label(start: datetime, today: date) -> str:
    return f"the {start:%H:%M} candle" if start.date() == today else f"the {start:%d %b %H:%M} candle"


async def evaluate(ctx) -> None:
    now_utc = ctx.now
    now_ist = now_utc.astimezone(IST)
    today = now_ist.date()
    first_close = datetime.combine(today, FIRST_CLOSE, tzinfo=IST)
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
    following = next_decision(now_ist)

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

    def next_text() -> str:
        when = f"next decision at the {following:%H:%M} close" + ("" if following.date() == today else f" {following:%a %d %b}")
        if position is None or "level" not in position:
            return when
        under = position["option_type"] == "CE"
        return f"{when}: against {position['level']:,.2f}, the stop a close {'under' if under else 'over'} {position['stop']:,.2f}"

    def wake() -> None:
        ctx.wake_at(following)
        held = state.get("position")
        if held and trading and now_ist < day_close:
            ctx.wake_at(day_close)
        if held and trading and now_ist < roll_at and held["weekly_expiry"] == today.isoformat():
            ctx.wake_at(roll_at)

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
        if position is not None and trading and expiry <= today:
            option = position["option"]
            price, pnl = await close("expiry_day_close")
            ctx.note("exited", signal="SELL", reason=(
                f"15:29 on its expiry day, not rolled at {ROLL_AT:%H:%M}: {option} sold at {price:,.2f}; P&L {pnl:+,.0f} before "
                f"charges; next decision {following:%a %d %b} at the {FIRST_CLOSE:%H:%M} close"
            ))
            return
        if position is not None:
            ctx.note("hold", reason=f"carried overnight: {held_text()}; {next_text()}")
            ctx.wake_at(following)
            return
        ctx.note("skipped", reason=f"day done: nothing bought after {DAY_CLOSE:%H:%M}; {next_text()}")
        return
    if now_ist < first_close:
        ctx.note("hold" if position else "skipped", reason=(f"{held_text()}; " if position else "") + next_text())
        wake()
        return

    # -- the rollover: on the held option's expiry day at 12:00 (or found expired), to next week's, same side
    if position is not None and (expiry < today or (expiry == today and now_ist >= roll_at)):
        option, side = position["option"], position["option_type"]
        carry = {k: position[k] for k in ("level", "candle", "low", "high") if k in position}
        price, pnl = await close("expiry_rollover")
        position = None
        due = now_utc + FLIP_GAP
        state["pending"] = {"option_type": side, "due": due.isoformat(), "why": f"{ROLL_AT:%H:%M} rollover", **carry}
        ctx.note("exited", signal="SELL", reason=(
            f"expiry rollover: {option} sold at {price:,.2f} (the bid), P&L {pnl:+,.0f} before charges; the same side's ATM "
            f"{SIDE_NAME[side]} on next week's weekly bought at {due.astimezone(IST):%H:%M:%S}"
        ))
        ctx.wake_at(due)
        return

    # -- the rule, once a candle: its close against the candle before's (O+H+L+C)/4
    newest, before, why = await closed_candles(ctx, nifty, now_ist)
    reading = None
    if newest is not None and state.get("decided_candle") != as_aware_utc(newest["ts"]).isoformat():
        start, prior = as_aware_utc(newest["ts"]).astimezone(IST), as_aware_utc(before["ts"]).astimezone(IST)
        level = round(level_of(before), 2)
        low, high, closed = float(before["low"]), float(before["high"]), float(newest["close"])
        side = wanted_side(closed, level)
        reading = (f"{label(start, today)} closed at {closed:,.2f} against {label(prior, today)}'s (O+H+L+C)/4 {level:,.2f} "
                   f"(low {low:,.2f}, high {high:,.2f})")
        # what the next close is measured against: the candle just closed
        upcoming = {"level": round(level_of(newest), 2), "candle": start.isoformat(),
                    "low": float(newest["low"]), "high": float(newest["high"])}
        state["decided_candle"] = as_aware_utc(newest["ts"]).isoformat()
        state["last_level"] = {"candle": prior.isoformat(), "level": level, "low": low, "high": high, "close": closed,
                               "closed_candle": start.isoformat(), "side": side, "at": now_ist.strftime("%H:%M:%S")}
        if position is not None and side is not None and side != position["option_type"]:
            held_side, option = position["option_type"], position["option"]
            stopped = closed <= low if held_side == "CE" else closed >= high
            price, pnl = await close("stop_loss" if stopped else "level_flip")
            position = None
            due = now_utc + FLIP_GAP
            state["pending"] = {"option_type": side, "due": due.isoformat(), "why": reading, **upcoming}
            crossed = f"past its {'low' if held_side == 'CE' else 'high'}: the stop" if stopped else f"now a {SIDE_NAME[side]}"
            ctx.note("exited", signal="SELL", reason=(
                f"{reading} -- {crossed}: {option} sold at {price:,.2f}, P&L {pnl:+,.0f} before charges; "
                f"the {SIDE_NAME[side]} bought at {due.astimezone(IST):%H:%M:%S}"
            ))
            ctx.wake_at(due)
            return
        if position is not None:
            position.update(level=upcoming["level"], candle=upcoming["candle"], low=upcoming["low"], high=upcoming["high"],
                            stop=upcoming["low"] if position["option_type"] == "CE" else upcoming["high"])
        else:
            pending = state.get("pending")
            if pending is not None and side is not None:
                pending.update(option_type=side, why=reading, **upcoming)
            elif pending is None and side is not None:
                state["pending"] = {"option_type": side, "due": now_utc.isoformat(), "why": reading, **upcoming}

    # -- a buy due: at once, or 5 seconds after a flip's sale
    pending = state.get("pending")
    if position is None and pending is not None:
        due = datetime.fromisoformat(pending["due"])
        side_name = SIDE_NAME[pending["option_type"]]
        if now_utc < due:
            ctx.note("skipped", reason=f"{reading or pending['why']}; the {side_name} is bought at {due.astimezone(IST):%H:%M:%S}")
            ctx.wake_at(due)
            return
        spot = await ctx.get_price(nifty.id)
        expiries = await ctx.list_weekly_expiries(nifty.id, today)
        weekly = (expiries[1] if expiries and today == expiries[0] and len(expiries) > 1 else expiries[0]) if expiries else None
        option_type = pending["option_type"]
        strike = atm_strike(spot) if spot is not None else None
        instrument = (await ctx.find_option(nifty.id, weekly, strike, option_type)
                      if weekly and weekly != today and strike is not None else None)
        quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
        if quote is None:
            wait = ("no live NIFTY price yet" if spot is None
                    else "no NIFTY weekly after today listed" if not weekly or weekly == today
                    else f"no {weekly:%d %b} {strike:,.0f} {option_type} in the catalog" if instrument is None
                    else f"no bid/ask for the {weekly:%d %b} {strike:,.0f} {option_type} yet")
            ctx.note("skipped", reason=f"{pending['why']}; buying the {side_name} waits: {wait}")
            ctx.wake_at(now_utc + RECHECK)
            return
        price = quote[1]
        quantity = float(LOTS * (instrument.lot_size or DEFAULT_LOT_SIZE))
        await ctx.open_leg(instrument, "buy", quantity, price)
        option = f"{weekly:%d %b} {strike:,.0f} {option_type}"
        state["position"] = position = {
            "regime": REGIME, "bias": f"long {side_name}", "option": option, "option_type": option_type,
            "strike": strike, "weekly_expiry": weekly.isoformat(), "entry_spot": spot, "decided_by": pending.get("why"),
            "legs": {"long": {"instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": "buy",
                              "expiry": weekly.isoformat(), "quantity": quantity, "entry_price": price}},
            "opened_at": now_utc.isoformat(),
        }
        if "level" in pending:
            position.update(level=pending["level"], candle=pending["candle"], low=pending["low"], high=pending["high"],
                            stop=pending["low"] if option_type == "CE" else pending["high"])
        state.pop("pending", None)
        ctx.note("entered", signal="BUY", reason=(
            f"{pending['why']} -- a {side_name}: bought {LOTS} lot of {option} at {price:,.2f} (the ask); "
            f"{next_text()}; held overnight, rolled at {ROLL_AT:%H:%M} on its expiry day"
        ))
        wake()
        return

    if newest is None:
        ctx.note("hold" if position else "skipped", reason=(f"{held_text()} kept; " if position else "") + why)
        ctx.wake_at(now_utc + RECHECK)
        if position is not None:
            ctx.wake_at(day_close)
        return
    if position is not None:
        ctx.note("hold", reason=((f"{reading} -- still a {SIDE_NAME[position['option_type']]}: " if reading else "")
                                 + f"{held_text()}; {next_text()}"))
    else:
        ctx.note("skipped", reason=(f"{reading} -- at the level: nothing bought yet; " if reading else "") + next_text())
    wake()
