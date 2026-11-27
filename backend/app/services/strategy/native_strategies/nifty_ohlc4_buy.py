"""
OHLC4 Buy 5 MIN -- one NIFTY option, checked every 5 minutes against the previous 15-minute candle's (O+H+L+C)/4 (native)
=========================================================================================================================

Version 2 (27 Nov): the sides flipped (agreed the same day) -- NIFTY above
the LEVEL holds a PUT, below it a CALL -- with a real stop: a CALL is sold
when a check comes in under the 15-minute candle's low, a PUT when one comes
in over its high, and after a stop nothing is bought until the next 15-
minute candle gives a new LEVEL. Version 1's table and stop note below are
the other way round; the rest as version 1.

  NIFTY above LEVEL     hold a PUT    -- its stop a check over the 15-minute candle's high
  NIFTY below LEVEL     hold a CALL   -- its stop a check under the 15-minute candle's low
  NIFTY at LEVEL        no change

Version 1 (27 Nov): as OI Change Buy 15 MIN (nifty_oi_change_buy.py) but with
one condition in place of the change in OI; after the backtest the same
day, checked at each 5-minute close against the 15-minute level (its best
reading: ohlc4_buy_backtest.py, "15m level, every 5m"):

  LEVEL   the previous 15-minute NIFTY candle's (open + high + low +
          close) / 4 -- the latest finished by the check; before 09:30 the
          session before's last (15:15-15:30)
  CHECK   at the 09:15 open (live NIFTY, once a session), then at each
          5-minute close 09:20-15:25 (that 5-minute candle's close)

  NIFTY above LEVEL     hold a CALL   -- its stop the 15-minute candle's low
  NIFTY below LEVEL     hold a PUT    -- its stop the 15-minute candle's high
  NIFTY at LEVEL        no change

A check is made once its candles are in -- the candle sync stores each 10-
70 s after its close; until then, what's held kept. Between checks nothing
is decided: what's held is held.

Always on one side (agreed 27 Nov: a flip at once): holding the call and a
check under LEVEL, the call is sold at the bid and, FLIP_GAP (5 seconds)
later, the ATM put bought at the ask -- the same the other way. The stop
is part of it: the 15-minute candle's low is always under its LEVEL, so a
check past the stop is also past LEVEL; such a sale is recorded as
"stop_loss", any other as "level_flip". Nothing held and a side decided,
it's bought at once.

What it buys: LOTS (1) lot of the ATM option -- the STRIKE_STEP (50-point)
strike nearest NIFTY at that moment -- on the nearest weekly expiry (next
week's on that weekly's own expiry day), at the ask. No trailing stop (as
agreed), no take-profit.

Held overnight, as OI Change Buy version 7: nothing sold at 15:29 -- the
option held then is carried to the next session's 09:15 open. On the held
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
TIMEFRAME = "5m"  # the checks
BAR = timedelta(minutes=5)
LEVEL_TIMEFRAME = "15m"  # the level
LEVEL_BAR = timedelta(minutes=15)
STRIKE_STEP = 50
LOTS = 1
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
SESSION_OPEN = dtime(9, 15)  # the opening check, on live NIFTY
FIRST_CLOSE = dtime(9, 20)  # the session's first 5-minute candle closes: the first 5-minute check
LEVEL_FIRST_CLOSE = dtime(9, 30)  # the session's first 15-minute candle closes
LAST_DECISION = dtime(15, 25)  # the last close checked (15:20-15:25); the 15:30 one comes after DAY_CLOSE
LAST_CLOSE = dtime(15, 30)  # the session's last candles close
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


def wanted_side(value: float, level: float) -> str | None:
    """Version 2 (flipped): "PE" above the level, "CE" under it, None at it."""
    if value > level:
        return "PE"
    return "CE" if value < level else None


def stop_hit(option_type: str, value: float, low: float, high: float) -> bool:
    """Version 2: a CALL's stop a check under the 15-minute candle's low, a PUT's a check over its high."""
    return value < low if option_type == "CE" else value > high


def latest_close(now_ist: datetime, first: dtime = FIRST_CLOSE, bar: timedelta = BAR) -> datetime:
    """The latest close (IST) by `now_ist` on the `bar` grid: today's `first`-15:30, or before `first` the session
    before's 15:30. latest_close(now) is the 5-minute one; latest_close(now, LEVEL_FIRST_CLOSE, LEVEL_BAR) the 15-minute."""
    today = now_ist.date()
    start = datetime.combine(today, first, tzinfo=IST)
    if now_ist < start or not is_trading_day(today):
        return datetime.combine(previous_trading_day(today), LAST_CLOSE, tzinfo=IST)
    steps = int((now_ist - start) / bar)
    return min(start + steps * bar, datetime.combine(today, LAST_CLOSE, tzinfo=IST))


def next_decision(now_ist: datetime) -> datetime:
    """The next check after `now_ist`: today's 09:15 open, a 5-minute close 09:20-15:25, else the next session's open."""
    today = now_ist.date()
    if is_trading_day(today):
        if now_ist < datetime.combine(today, SESSION_OPEN, tzinfo=IST):
            return datetime.combine(today, SESSION_OPEN, tzinfo=IST)
        first = datetime.combine(today, FIRST_CLOSE, tzinfo=IST)
        if now_ist < first:
            return first
        following = latest_close(now_ist) + BAR
        if following <= datetime.combine(today, LAST_DECISION, tzinfo=IST):
            return following
    return datetime.combine(next_trading_day(today), SESSION_OPEN, tzinfo=IST)


def leg_pnl(leg: dict, exit_price: float) -> float:
    return (exit_price - leg["entry_price"]) * leg["quantity"]


def ends(bar: dict, length: timedelta) -> datetime:
    return (as_aware_utc(bar["ts"]) + length).astimezone(IST)


async def level_candle(ctx, nifty: Instrument, by: datetime) -> tuple[dict | None, str]:
    """(the latest 15-minute NIFTY candle finished by `by`, "") or (None, why not yet)."""
    expected = latest_close(by, LEVEL_FIRST_CLOSE, LEVEL_BAR)
    bars = [b for b in await ctx.get_candles(nifty.id, LEVEL_TIMEFRAME, 3) if ends(b, LEVEL_BAR) <= by]
    if not bars:
        return None, "no NIFTY 15-minute candles yet"
    if ends(bars[-1], LEVEL_BAR) < expected:
        return None, f"waiting for the NIFTY 15-minute candle to {expected:%H:%M} (newest ends {ends(bars[-1], LEVEL_BAR):%d %b %H:%M})"
    return bars[-1], ""


async def check_candle(ctx, nifty: Instrument, now_ist: datetime) -> tuple[dict | None, str]:
    """(the 5-minute NIFTY candle closed last, "") or (None, why not yet)."""
    expected = latest_close(now_ist)
    bars = await ctx.get_candles(nifty.id, TIMEFRAME, 2)
    if not bars:
        return None, "no NIFTY 5-minute candles yet"
    if ends(bars[-1], BAR) < expected:
        return None, f"waiting for the NIFTY 5-minute candle to {expected:%H:%M} (newest ends {ends(bars[-1], BAR):%d %b %H:%M})"
    return bars[-1], ""


async def bid_ask(ctx, instrument_id: uuid.UUID) -> tuple[float, float] | None:
    getter = None if getattr(ctx, "is_backtest", False) else getattr(ctx, "get_bid_ask", None)
    if getter is None:  # a backtest: no order book, the last price both ways
        price = await ctx.get_price(instrument_id)
        return (price, price) if price is not None else None
    return await getter(instrument_id)


def label(start: datetime, today: date) -> str:
    return f"the {start:%H:%M}" if start.date() == today else f"the {start:%d %b %H:%M}"


async def evaluate(ctx) -> None:
    now_utc = ctx.now
    now_ist = now_utc.astimezone(IST)
    today = now_ist.date()
    open_at = datetime.combine(today, SESSION_OPEN, tzinfo=IST)
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
        when = ("next check at the " + ("open" if following.time() == SESSION_OPEN else f"{following:%H:%M} close")
                + ("" if following.date() == today else f" {following:%a %d %b}"))
        if position is None or "level" not in position:
            return when
        under = position["option_type"] == "CE"
        return (f"{when}; the level {position['level']:,.2f} (the {position['candle_label']} 15-minute candle's, until the next "
                f"one), the stop NIFTY {'under' if under else 'over'} {position['stop']:,.2f}")

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
                f"charges; next check {following:%a %d %b} at the {SESSION_OPEN:%H:%M} open"
            ))
            return
        if position is not None:
            ctx.note("hold", reason=f"carried overnight: {held_text()}; {next_text()}")
            ctx.wake_at(following)
            return
        ctx.note("skipped", reason=f"day done: nothing bought after {DAY_CLOSE:%H:%M}; {next_text()}")
        return
    if now_ist < open_at:
        ctx.note("hold" if position else "skipped", reason=(f"{held_text()}; " if position else "") + next_text())
        wake()
        return

    # -- the rollover: on the held option's expiry day at 12:00 (or found expired), to next week's, same side
    if position is not None and (expiry < today or (expiry == today and now_ist >= roll_at)):
        option, side = position["option"], position["option_type"]
        carry = {k: position[k] for k in ("level", "candle", "candle_label", "low", "high") if k in position}
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

    # -- the check: at the open (live NIFTY, once a session), then each 5-minute close, against the 15-minute level
    value, check_key, what, why = None, None, None, ""
    if now_ist < first_close:
        if state.get("opened_on") != today.isoformat():
            value = await ctx.get_price(nifty.id)
            check_key, what = f"open {today.isoformat()}", "NIFTY {value} at the open"
            why = "" if value is not None else "no live NIFTY price yet"
        by = now_ist
    else:
        candle, why = await check_candle(ctx, nifty, now_ist)
        if candle is not None:
            value, check_key = float(candle["close"]), as_aware_utc(candle["ts"]).isoformat()
            what = f"the {as_aware_utc(candle['ts']).astimezone(IST):%H:%M} 5-minute candle closed at {{value}}"
            by = ends(candle, BAR)
    bar = None
    if value is not None and check_key != state.get("decided_candle"):
        bar, why = await level_candle(ctx, nifty, by)
    reading = None
    if bar is not None:
        start = as_aware_utc(bar["ts"]).astimezone(IST)
        level = round(level_of(bar), 2)
        low, high = float(bar["low"]), float(bar["high"])
        side = wanted_side(value, level)
        reading = (what.format(value=f"{value:,.2f}") + f" against {label(start, today)} 15-minute candle's (O+H+L+C)/4 "
                   f"{level:,.2f} (low {low:,.2f}, high {high:,.2f})")
        rule = {"level": level, "candle": start.isoformat(), "candle_label": f"{start:%H:%M}" if start.date() == today
                else f"{start:%d %b %H:%M}", "low": low, "high": high}
        state["decided_candle"] = check_key
        if check_key.startswith("open "):
            state["opened_on"] = today.isoformat()
        state["last_level"] = {**rule, "value": value, "checked": check_key, "side": side, "at": now_ist.strftime("%H:%M:%S")}
        if state.get("stopped_on") not in (None, rule["candle"]):
            state.pop("stopped_on")  # version 2: a new 15-minute level -- the rule again after a stop
        if position is not None and stop_hit(position["option_type"], value, low, high):  # version 2: the stop
            held_side, option = position["option_type"], position["option"]
            price, pnl = await close("stop_loss")
            position = None
            state.pop("pending", None)
            state["stopped_on"] = rule["candle"]
            ctx.note("exited", signal="SELL", reason=(
                f"{reading} -- {'under its low' if held_side == 'CE' else 'over its high'}: the stop; {option} sold at "
                f"{price:,.2f}, P&L {pnl:+,.0f} before charges; nothing bought until the next 15-minute candle's level"
            ))
            wake()
            return
        if position is not None and side is not None and side != position["option_type"]:
            option = position["option"]
            price, pnl = await close("level_flip")
            position = None
            due = now_utc + FLIP_GAP
            state["pending"] = {"option_type": side, "due": due.isoformat(), "why": reading, **rule}
            ctx.note("exited", signal="SELL", reason=(
                f"{reading} -- now a {SIDE_NAME[side]}: {option} sold at {price:,.2f}, P&L {pnl:+,.0f} before charges; "
                f"the {SIDE_NAME[side]} bought at {due.astimezone(IST):%H:%M:%S}"
            ))
            ctx.wake_at(due)
            return
        if position is not None:
            position.update(rule, stop=low if position["option_type"] == "CE" else high)
        elif state.get("stopped_on") == rule["candle"]:  # version 2: flat after a stop until a new level
            reading = f"{reading} -- flat after the stop until the next 15-minute candle's level"
        else:
            pending = state.get("pending")
            if pending is not None and side is not None:
                pending.update(option_type=side, why=reading, **rule)
            elif pending is None and side is not None:
                state["pending"] = {"option_type": side, "due": now_utc.isoformat(), "why": reading, **rule}

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
            position.update({k: pending[k] for k in ("level", "candle", "candle_label", "low", "high")},
                            stop=pending["low"] if option_type == "CE" else pending["high"])
        state.pop("pending", None)
        ctx.note("entered", signal="BUY", reason=(
            f"{pending['why']} -- a {side_name}: bought {LOTS} lot of {option} at {price:,.2f} (the ask); "
            f"{next_text()}; held overnight, rolled at {ROLL_AT:%H:%M} on its expiry day"
        ))
        wake()
        return

    if why:  # a check due but its candle (or NIFTY) not in yet: what's held kept
        ctx.note("hold" if position else "skipped", reason=(f"{held_text()} kept; " if position else "") + why)
        ctx.wake_at(now_utc + RECHECK)
        if position is not None:
            ctx.wake_at(day_close)
        return
    if position is not None:
        ctx.note("hold", reason=((f"{reading} -- still a {SIDE_NAME[position['option_type']]}: " if reading else "")
                                 + f"{held_text()}; {next_text()}"))
    elif state.get("stopped_on"):  # version 2: flat after a stop
        ctx.note("skipped", reason=(f"{reading}; " if reading else "flat after the stop until the next 15-minute candle's level; ")
                 + next_text())
    else:
        ctx.note("skipped", reason=(f"{reading} -- at the level: nothing bought yet; " if reading else "") + next_text())
    wake()
