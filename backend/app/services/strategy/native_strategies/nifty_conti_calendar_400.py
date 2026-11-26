"""
Conti Calendar 400 15 MIN -- a double calendar on the OTM strikes nearest a Rs 100 premium (native)
==================================================================================================

Conti Calendar 15 MIN copied with the amendments agreed 11 Nov (version 2,
11 Nov: the pause after a close cut to 1 minute; version 3, 12 Nov: the
strikes by premium, not 400 points out; version 4, 16 Nov: the next weekly
when the nearest has no Rs 100 strike; version 5, 19 Nov: a monthly cover
2.5-3x the weekly's bid; version 6, 20 Nov: 20 lots a leg and the strike
shift below; version 7, 20 Nov: the target restarts at a shift; version 8,
20 Nov: a shift's closed legs a trade of their own; version 9, 26 Nov: the
hedge a weekly two weeks out at the same strike and the roll at 15:00 the
day before expiry -- a shift at the profit chart's peak, also considered,
left out after the backtest of 26 Nov): instead of one
strike's straddle calendar, a call calendar on an OTM call and a put
calendar on an OTM put, LOTS_PER_LEG (20; 16 before version 6) lots each,
every fill at the order book's best price (a sell at the bid, a buy at the
ask):

  sell  this week's weekly CE at the call strike   buy  the weekly two weeks out CE at it
  sell  this week's weekly PE at the put strike    buy  the weekly two weeks out PE at it

-- e.g. sell the 1 Dec weeklies, buy the 15 Dec ones at the same strikes.

The strikes (version 3; ATM+-400 in hundreds before): on each side, the
furthest OTM strike -- any STRIKE_STEP (50-point) strike, calls above NIFTY
and puts below it, however far out -- whose weekly's bid (what it sells at)
is still MIN_PREMIUM (Rs 100) or more: walked out from the first OTM strike
until a bid falls under Rs 100, so both sold premiums sit just above
Rs 100, close to each other. NIFTY 25,300 with the 25,600 CE bid 118 and
the 25,650 CE 96, the 25,000 PE 104 and the 24,950 PE 88: the 25,600 CE
and the 25,000 PE. A strike on the way with no book yet means no entry that
check (the next tries again); none at Rs 100 or more on a side, none at all.
Every new position is picked so -- the hedge roll's reopen too; a weekly
roll keeps its position's strikes.

The hedge (version 9, 26 Nov; a monthly before -- at the same strike, then
from version 5 at the strike whose ask was 2.5-3x the weekly's bid): the
weekly two weeks after the sold one -- the listed expiry HEDGE_DAYS (11 to
17) days after it, nearest 14, so an expiry brought forward by a holiday
still counts -- at the sold leg's own strike, bought at the ask. None
listed, or one of its strikes with no book, means no entry that check. A
position from before (a monthly hedge: no "hedge_rule") is closed at bid /
ask at the first check inside the entry window ("rule_change") and a new
one opened at once by the new rule.

Weekly: the nearest weekly expiry whose roll day (ROLL_TIME's day: the
trading day before it, version 9) is still to come -- on that day and on
the expiry day itself, the one after -- or (version 4) the expiry after it
when that one has, on either side, no OTM strike whose bid is still
MIN_PREMIUM: both sides then sold on that later expiry at its Rs 100
strikes, the hedge two weeks after it. Only that one step: none there
either, no entry that check. A strike on the way with no book is no reason
to move on -- the next check tries the nearest again.

Opened from 09:30 (ENTRY_TIME) to 15:00 whenever none is open, and never
within PAUSE_AFTER_CLOSE (1 minute; 5 in version 1) of the last one closing (kept in the
state, paused_until, so a restart doesn't cut it short).

Held overnight -- no daily close -- until the take-profit:

  Take-profit  the whole position since it opened, at bid / ask after
               charges: what closing the open legs right now would give
               (sold legs bought back at the ask, bought legs sold at the
               bid, less the closing orders' charges and the open legs'
               opening charges) plus what the weekly rolls have booked
               (their P&L less both their orders' charges). At TAKE_PROFIT
               (Rs 14,000) or more, every open leg closes at those prices;
               the next opens once the pause is up. No stop-loss. After a
               strike shift (version 7, 20 Nov) the target restarts: what
               rolls booked before the shift (booked_outside_target) is
               left out, and "Net if closed now" -- the figure the
               take-profit reads -- counts from the shift: the legs held
               since, with anything booked after, until Rs 14,000.
  Strike shift (version 6) NIFTY gone more than SHIFT_IN_LOSS (150) points
               toward a side -- up for the call side, down for the put
               side -- from where NIFTY was when that side was opened or
               last shifted, the position in loss ("Net if closed now" as
               the take-profit reads it -- since the last shift, version 7 --
               under Rs 0); SHIFT_IN_PROFIT (200)
               points when in profit but short of the target: that side's
               sold weekly and bought hedge close at bid / ask and the side
               opens again at NIFTY's Rs 100 strike now, its hedge at the
               same strike (version 9), on the position's same
               weekly and hedge expiries and at its own lots (a 16-lot
               position stays 16);
               the other side is left as it is, and the shifted side's
               reference is NIFTY at the shift. The closed legs (version 8,
               20 Nov) are written at once as a trade of their own
               ("strike_shift", from that side's opening to the shift; no
               Telegram alert for it) -- in the trade history then, and no
               longer part of the position or its final trade. Only from 09:30 to 15:00,
               read every second; a shift that can't be had (no Rs 100
               strike, a leg unquoted) waits and is tried
               again, and one whose Rs 100 strike hasn't moved just moves
               the reference.
  Weekly roll  at ROLL_TIME (15:00) on the trading day before the sold
               weeklies' expiry (version 9; on the expiry day itself
               before) -- or at the first check after, if the app was down
               then -- the two sold weeklies are bought back at the ask and
               the next weekly sold at the bid at the SAME strikes; the
               bought hedges stay. Their result is booked into the position.
  Hedge roll   (version 9; the monthly roll before) when the next weekly
               would be the bought hedges' own expiry -- every second week
               -- the roll instead closes all four ("hedge_roll") and opens
               a fresh double calendar at once -- at that moment's Rs 100
               strikes, selling the next weekly and buying two weeks after
               it -- whatever the time and pause (state reopen_due: a
               reopen that can't be had at once, a leg unquoted, is tried
               again at every check until it is).

A trade record is the whole position (less its strike shifts' legs, each
shift a trade of its own, version 8): every leg it held, rolled ones
included, its P&L across them, and -- the extremes -- the most profit and
the most loss "if closed now" reached while it was open (with when), also
kept on the position for the card (max_profit_reached / max_loss_reached).

Checked every RECHECK (1 second) while a position is open (ctx.wake_at),
on top of the scheduler's 10-second cycle. A leg with no bid or no ask
means no take-profit reading that check. In a backtest there is no order
book (ctx.is_backtest): the last price stands for both bid and ask. Runs
through services/paper_trading/native_runner.py.
"""

import inspect
import math
import uuid
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.backfill_platform.coverage import previous_trading_day
from app.services.broker.zerodha_broker import IST
from app.services.paper_trading.trade_record import order_charges

UNDERLYING_SYMBOL = "NIFTY 50"
REGIME = "double calendar"
STRIKE_STEP = 50  # any 50-point strike (version 3; hundreds, ATM+-400, before)
MIN_PREMIUM = 100.0  # rupees: the furthest OTM strike whose sold weekly's bid is still this much (version 3)
MAX_STEPS = 40  # how many strikes out it looks on a side: 2,000 points
HEDGE_DAYS = (11, 17)  # version 9: the bought hedge's expiry this many days after the sold weekly's, nearest 14
HEDGE_RULE = "the weekly two weeks out, same strike"  # kept on a position opened by version 9's rule
EXPIRIES_READ = 8  # listed expiries read: the sold weekly's, the next, and two weeks past either
LOTS_PER_LEG = 20  # version 6 (20 Nov); 16 before
SHIFT_IN_LOSS = 150.0  # version 6: NIFTY points toward a side, from its reference, that shift it -- the position in loss
SHIFT_IN_PROFIT = 200.0  # the same, in profit but short of the take-profit
SIDE_LEGS = {"CE": ("short_ce", "long_ce"), "PE": ("short_pe", "long_pe")}
SIDE_NAME = {"CE": "call", "PE": "put"}
SHIFT_EXIT = "strike_shift"  # version 8: a shift's closed legs, a trade of their own (no Telegram alert)
HEDGE_EXIT = "hedge_roll"  # version 9: every second week all four close and a fresh one opens ("monthly_roll" before)
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 30)
ENTRY_UNTIL = dtime(15, 0)
ROLL_TIME = dtime(15, 0)  # on the trading day before the sold weeklies' expiry (version 9; on the expiry day before)
RECHECK = timedelta(seconds=1)  # while a position is open: how soon it's checked again
PAUSE_AFTER_CLOSE = timedelta(minutes=1)  # no new position this soon after one closes (version 2; 5 minutes in 1)
TAKE_PROFIT = 14_000.0  # rupees, the whole position after charges, at bid / ask
LEGS = (  # name, option type, side at entry, which expiry
    ("short_ce", "CE", "sell", "weekly"),
    ("long_ce", "CE", "buy", "hedge"),
    ("short_pe", "PE", "sell", "weekly"),
    ("long_pe", "PE", "buy", "hedge"),
)
WEEKLY_LEGS = ("short_ce", "short_pe")


def first_otm(spot: float, option_type: str) -> float:
    """The nearest STRIKE_STEP strike out of the money: a call's above NIFTY, a put's below."""
    if option_type == "CE":
        return (math.floor(spot / STRIKE_STEP) + 1) * STRIKE_STEP
    return (math.ceil(spot / STRIKE_STEP) - 1) * STRIKE_STEP


def roll_day(weekly: date) -> date:
    """Version 9: the trading day before `weekly` -- the weeklies sold on it roll at ROLL_TIME that day."""
    return previous_trading_day(weekly)


def roll_moment(weekly: date) -> datetime:
    """When the weeklies sold on `weekly` roll: ROLL_TIME (IST) on its roll day."""
    return datetime.combine(roll_day(weekly), ROLL_TIME, tzinfo=IST)


def hedge_after(expiries: list[date], weekly: date) -> date | None:
    """Version 9: the listed expiry two weeks after `weekly` -- HEDGE_DAYS (11 to 17) days on, nearest 14 -- or None."""
    lo, hi = HEDGE_DAYS
    near = [e for e in expiries if lo <= (e - weekly).days <= hi]
    return min(near, key=lambda e: abs((e - weekly).days - 14)) if near else None


def hedge_of(position: dict) -> date:
    """The bought legs' expiry (a position from before version 9: its monthly's)."""
    return date.fromisoformat(position.get("hedge_expiry") or position["monthly_expiry"])


def closing_price(leg: dict, bid: float, ask: float) -> float:
    """What closing `leg` fills at: a sold leg is bought back at the ask, a bought one sold at the bid."""
    return ask if leg["side"] == "sell" else bid


def leg_pnl(leg: dict, exit_price: float) -> float:
    diff = leg["entry_price"] - exit_price if leg["side"] == "sell" else exit_price - leg["entry_price"]
    return diff * leg["quantity"]


def charges_of(leg: dict, price: float, buying: bool) -> float:
    return order_charges({"option_type": leg["option_type"], "quantity": leg["quantity"]}, price, buying, False) or 0.0


def booked_net(position: dict) -> float:
    """What the weekly rolls have booked: their P&L less both their orders' charges."""
    booked = position.get("booked") or {}
    return float(booked.get("gross", 0.0)) - float(booked.get("charges", 0.0))


def if_closed_now(position: dict, quotes: dict[str, tuple[float, float]]) -> tuple[float, dict[str, float]]:
    """(the whole position's profit after every charge, each open leg's
    closing price) were the open legs closed now at `quotes` (name -> (bid, ask))."""
    gross, exit_charges, prices = 0.0, 0.0, {}
    for name, leg in position["legs"].items():
        bid, ask = quotes[name]
        price = closing_price(leg, bid, ask)
        prices[name] = price
        gross += leg_pnl(leg, price)
        exit_charges += charges_of(leg, price, buying=leg["side"] == "sell")
    return booked_net(position) + gross - position["entry_charges"] - exit_charges, prices


def track_extremes(position: dict, net: float, now_ist: datetime) -> None:
    """The most profit and the most loss "if closed now" reached so far, with when."""
    when, net = now_ist.strftime("%d %b %H:%M:%S IST"), round(net, 2)  # to the paisa, as kept: an equal reading isn't a new extreme
    if position.get("max_profit_reached") is None or net > position["max_profit_reached"]:
        position["max_profit_reached"], position["max_profit_at"] = net, when
    if position.get("max_loss_reached") is None or net < position["max_loss_reached"]:
        position["max_loss_reached"], position["max_loss_at"] = net, when


def extremes_of(position: dict) -> dict | None:
    if position.get("max_profit_reached") is None:
        return None
    return {key: position.get(key) for key in ("max_profit_reached", "max_profit_at", "max_loss_reached", "max_loss_at")}


async def bid_ask(ctx, instrument_id: uuid.UUID) -> tuple[float, float] | None:
    getter = None if getattr(ctx, "is_backtest", False) else getattr(ctx, "get_bid_ask", None)
    if getter is None:  # a backtest: no order book, the last price both ways
        price = await ctx.get_price(instrument_id)
        return (price, price) if price is not None else None
    return await getter(instrument_id)


def shift_ref(position: dict, option_type: str, spot: float) -> float:
    """Where NIFTY was when that side was opened or last shifted (version 6)."""
    return float(position.get(f"shift_ref_{option_type.lower()}") or position.get("entry_spot") or spot)


def outside_target(position: dict) -> float:
    """Version 7: what was booked up to the last shift -- left out of the take-profit,
    which restarts at Rs 14,000 at every shift. A position shifted under version 6
    (no rolls since): everything it has booked."""
    if position.get("booked_outside_target") is None and position.get("shifts"):
        position["booked_outside_target"] = round(float(position.get("booked_net") or 0.0), 2)
    return float(position.get("booked_outside_target") or 0.0)


def shift_text(position: dict) -> str:
    """The levels that shift a side, for the card."""
    net = position.get("net_if_closed_now") or 0.0
    trigger = SHIFT_IN_LOSS if net < 0 else SHIFT_IN_PROFIT
    spot = position.get("entry_spot") or 0.0
    text = (f"shifts the call side over NIFTY {shift_ref(position, 'CE', spot) + trigger:,.0f}, the put side under "
            f"{shift_ref(position, 'PE', spot) - trigger:,.0f} ({trigger:,.0f} points, {'in loss' if net < 0 else 'in profit'})")
    return text + (f"; {position['shift_waits']}" if position.get("shift_waits") else "")


async def side_strike(ctx, underlying_id: uuid.UUID, expiry: date, spot: float, option_type: str) -> tuple[float | None, str, bool]:
    """(the furthest OTM `option_type` strike whose `expiry` bid is still
    MIN_PREMIUM or more, "", False) -- walked out from the first OTM strike
    until a bid falls under it -- or (None, why not, whether it was a strike
    with no book yet)."""
    found, step = None, STRIKE_STEP if option_type == "CE" else -STRIKE_STEP
    for k in range(MAX_STEPS):
        strike = first_otm(spot, option_type) + k * step
        instrument = await ctx.find_option(underlying_id, expiry, strike, option_type)
        if instrument is None:
            break  # past the catalog's last strike
        quote = await bid_ask(ctx, instrument.id)
        if quote is None:
            return None, f"no bid/ask for the {expiry:%d %b} {strike:,.0f} {option_type} yet", True
        if quote[0] < MIN_PREMIUM:
            break
        found = strike
    if found is None:
        return None, f"no OTM {expiry:%d %b} {option_type} with a bid of Rs {MIN_PREMIUM:,.0f} or more", False
    return found, "", False


async def evaluate(ctx) -> None:
    now_utc = ctx.now
    now_ist = now_utc.astimezone(IST)
    today = now_ist.date()

    underlying = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))
    ).scalar_one_or_none()
    if underlying is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return

    force_exit = ctx.state.pop("force_exit", False)
    position = ctx.state.get("position")

    async def quote_or_last(leg: dict) -> float:
        """A leg's closing price that must be had now: bid / ask, else the
        last price, else (an expired contract nothing prices) its entry."""
        iid = uuid.UUID(leg["instrument_id"])
        quote = await bid_ask(ctx, iid)
        if quote is not None:
            return closing_price(leg, *quote)
        last = await ctx.get_price(iid)
        return last if last is not None else leg["entry_price"]

    def trade_leg(leg: dict, exit_price: float) -> dict:
        return {"instrument_id": leg["instrument_id"], "side": "short" if leg["side"] == "sell" else "long",
                "quantity": leg["quantity"], "entry_price": leg["entry_price"], "exit_price": exit_price}

    async def close_all(exit_reason: str, prices: dict[str, float]) -> float:
        exit_charges = sum(charges_of(leg, prices[name], buying=leg["side"] == "sell") for name, leg in position["legs"].items())
        closing_net = (booked_net(position) + sum(leg_pnl(leg, prices[name]) for name, leg in position["legs"].items())
                       - position["entry_charges"] - exit_charges)
        track_extremes(position, closing_net, now_ist)  # the close itself counts
        trade_legs = list(position.get("closed_legs") or [])
        total = float((position.get("booked") or {}).get("gross", 0.0))
        for name, leg in position["legs"].items():
            instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
            price = prices[name]
            await ctx.close_leg(instrument, "buy" if leg["side"] == "sell" else "sell", leg["quantity"], price)
            total += leg_pnl(leg, price)
            trade_legs.append(trade_leg(leg, price))
        outlay = sum(leg["entry_price"] * leg["quantity"] for leg in trade_legs)
        extra = {}
        extremes = extremes_of(position)
        if extremes is not None and "extremes" in inspect.signature(ctx.record_trade).parameters:
            extra["extremes"] = extremes
        await ctx.record_trade(
            legs=trade_legs, pnl=total, pnl_pct=(total / outlay * 100) if outlay else 0.0, exit_reason=exit_reason,
            opened_at=datetime.fromisoformat(position["opened_at"]), closed_at=now_utc, **extra,
        )
        ctx.state["position"] = None
        ctx.state["paused_until"] = (now_utc + PAUSE_AFTER_CLOSE).isoformat()
        return total

    async def forced_prices() -> dict[str, float]:
        return {name: await quote_or_last(leg) for name, leg in position["legs"].items()}

    def extremes_text() -> str:
        e = extremes_of(position) or {}
        if not e:
            return ""
        return (f"; max profit reached {e['max_profit_reached']:+,.0f} ({e['max_profit_at']}), "
                f"max loss reached {e['max_loss_reached']:+,.0f} ({e['max_loss_at']})")

    async def maybe_shift(net: float) -> str | None:
        """Version 6: the side NIFTY has moved toward, SHIFT_IN_LOSS (in loss) or
        SHIFT_IN_PROFIT (in profit) points past its reference, shifted; the note,
        or None (nothing due, or a shift that can't be had this check)."""
        spot = await ctx.get_price(underlying.id)
        if spot is None:
            return None
        trigger = SHIFT_IN_LOSS if net < 0 else SHIFT_IN_PROFIT
        for option_type in ("CE", "PE"):
            side, ref = SIDE_NAME[option_type], shift_ref(position, option_type, spot)
            moved = spot - ref if option_type == "CE" else ref - spot
            if moved <= trigger:
                continue
            why = (f"NIFTY {spot:,.1f} is {moved:,.0f} points {'above' if option_type == 'CE' else 'below'} the {side} side's "
                   f"{ref:,.1f}, past {trigger:,.0f}")
            note = await shift_side(option_type, spot, net, why)
            if note is not None:
                return note
        return None

    async def shift_side(option_type: str, spot: float, net: float, why: str) -> str | None:
        """That side's sold weekly and bought hedge closed at bid / ask -- a trade
        of their own -- and the side opened again at NIFTY's Rs 100 strike now,
        its hedge at the same strike (version 9), on the same expiries."""
        lower, side = option_type.lower(), SIDE_NAME[option_type]
        weekly_name, hedge_name = SIDE_LEGS[option_type]
        weekly, hedge = date.fromisoformat(position["weekly_expiry"]), hedge_of(position)
        strike, why_not, _ = await side_strike(ctx, underlying.id, weekly, spot, option_type)
        if strike is not None and strike == position["legs"][weekly_name]["strike"]:  # the Rs 100 strike hasn't moved: nothing to shift, the reference does
            position[f"shift_ref_{lower}"] = spot
            return None
        fills: dict[str, tuple[Instrument, float]] = {}
        for name, expiry, at_ask in ((weekly_name, weekly, False), (hedge_name, hedge, True)):
            if strike is None:
                break
            instrument = await ctx.find_option(underlying.id, expiry, strike, option_type)
            quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
            if quote is None:
                why_not = (f"no {expiry:%d %b} {strike:,.0f} {option_type} in the catalog" if instrument is None
                           else f"no bid/ask for the {expiry:%d %b} {strike:,.0f} {option_type} yet")
                break
            fills[name] = (instrument, quote[1] if at_ask else quote[0])
        if len(fills) < 2:
            position["shift_waits"] = f"shifting the {side} side waits: {why_not}"
            return None
        out, shift_legs, shift_pnl = [], [], 0.0
        for name in (weekly_name, hedge_name):
            leg = position["legs"][name]
            exit_price = await quote_or_last(leg)
            old = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
            await ctx.close_leg(old, "buy" if leg["side"] == "sell" else "sell", leg["quantity"], exit_price)
            shift_pnl += leg_pnl(leg, exit_price)
            shift_legs.append(trade_leg(leg, exit_price))
            position["entry_charges"] = round(position["entry_charges"] - leg.get("entry_charges", 0.0), 2)
            out.append(f"{date.fromisoformat(leg['expiry']):%d %b} {leg['strike']:,.0f} {option_type} {exit_price:,.2f}")
        # version 8: the closed legs are a trade of their own -- in the history now, not in the position's
        outlay = sum(leg["entry_price"] * leg["quantity"] for leg in shift_legs)
        await ctx.record_trade(
            legs=shift_legs, pnl=shift_pnl, pnl_pct=(shift_pnl / outlay * 100) if outlay else 0.0, exit_reason=SHIFT_EXIT,
            opened_at=datetime.fromisoformat(position.get(f"side_opened_at_{lower}") or position["opened_at"]), closed_at=now_utc,
        )
        for name, order_side, expiry in ((weekly_name, "sell", weekly), (hedge_name, "buy", hedge)):
            instrument, price = fills[name]
            quantity = position["legs"][name]["quantity"]  # the position's own lots, whatever LOTS_PER_LEG is now
            await ctx.open_leg(instrument, order_side, quantity, price)
            leg = {"instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": order_side,
                   "expiry": expiry.isoformat(), "quantity": quantity, "entry_price": price}
            leg["entry_charges"] = round(charges_of(leg, price, buying=order_side == "buy"), 2)
            position["entry_charges"] = round(position["entry_charges"] + leg["entry_charges"], 2)
            position["legs"][name] = leg
        position.update({
            "booked_outside_target": round(booked_net(position), 2),  # version 7: the target restarts here (rolls before it left out)
            "shift_trades": True, f"side_opened_at_{lower}": now_utc.isoformat(), "last_shift_pnl": round(shift_pnl, 2),
            f"strike_{lower}": strike, f"shift_ref_{lower}": spot, "shifts": int(position.get("shifts", 0)) + 1,
            "shifted_at": now_utc.isoformat(),
        })
        position.pop("shift_waits", None)
        return (f"strike shift, {'in loss' if net < 0 else 'in profit'} (Rs {net:,.0f} if closed now): {why} -- "
                f"{' and '.join(out)} closed at bid/ask; {weekly:%d %b} {strike:,.0f} {option_type} sold at the bid "
                f"{fills[weekly_name][1]:,.2f} and {hedge:%d %b} {strike:,.0f} {option_type} bought at the ask {fills[hedge_name][1]:,.2f}; "
                f"the other side kept; the closed legs P&L {shift_pnl:+,.0f} before charges, recorded as a trade; "
                f"the take-profit restarts at Rs {TAKE_PROFIT:,.0f}")

    if position is not None and position.get("shifts") and not position.get("shift_trades"):
        # versions 6-7 kept a shift's closed legs inside the position: with no roll since, they're all the shifts'
        if int(position.get("rolls", 0)) == 0 and position.get("closed_legs"):
            legs = list(position["closed_legs"])
            pnl = float((position.get("booked") or {}).get("gross", 0.0))
            outlay = sum(leg["entry_price"] * leg["quantity"] for leg in legs)
            await ctx.record_trade(
                legs=legs, pnl=pnl, pnl_pct=(pnl / outlay * 100) if outlay else 0.0, exit_reason=SHIFT_EXIT,
                opened_at=datetime.fromisoformat(position["opened_at"]),
                closed_at=datetime.fromisoformat(position.get("shifted_at") or now_utc.isoformat()),
            )
            position.update(booked={"gross": 0.0, "charges": 0.0}, booked_net=0.0, closed_legs=[], booked_outside_target=0.0,
                            last_shift_pnl=round(pnl, 2))
        position["shift_trades"] = True

    rolled_note = ""
    if (position is not None and not force_exit and position.get("hedge_rule") != HEDGE_RULE
            and ENTRY_TIME <= now_ist.time() < ENTRY_UNTIL):
        # version 9: a position from before -- a monthly hedge -- closes and a new one opens at once by the new hedge rule
        pnl = await close_all("rule_change", await forced_prices())
        ctx.state.pop("paused_until", None)
        ctx.state["reopen_due"] = True
        rolled_note = (f"the hedge rule changed (version 9: the weekly two weeks out at the same strike, not a monthly): "
                       f"the position closed, P&L {pnl:+,.0f} before charges; ")
        position = None
    if position is not None:
        position.pop("shift_waits", None)  # read afresh each check
        if force_exit:
            pnl = await close_all("manual", await forced_prices())
            ctx.note("exited", signal="COVER", reason=(
                f"manual exit; P&L {pnl:+,.0f} before charges{extremes_text()}; next entry from {now_ist + PAUSE_AFTER_CLOSE:%H:%M:%S}"
            ))
            ctx.wake_at(now_utc + PAUSE_AFTER_CLOSE)
            return

        quotes, missing = {}, []
        for name, leg in position["legs"].items():
            quote = await bid_ask(ctx, uuid.UUID(leg["instrument_id"]))
            if quote is None:
                missing.append(name)
            else:
                quotes[name] = quote
        if not missing:
            net, prices = if_closed_now(position, quotes)
            net -= outside_target(position)  # versions 7-8: from the last shift -- the figure the take-profit reads
            position.pop("net_toward_target", None)  # version 7's second figure: one now
            position["net_if_closed_now"] = round(net, 2)
            position["checked_at"] = now_ist.strftime("%d %b %H:%M:%S IST")
            track_extremes(position, net, now_ist)
            if net >= TAKE_PROFIT:
                ext = extremes_text()
                pnl = await close_all("take_profit", prices)
                since = " since the last shift" if position.get("shifts") else ""
                ctx.note("exited", signal="COVER", reason=(
                    f"take-profit: Rs {net:,.0f} after charges at bid/ask{since} (target Rs {TAKE_PROFIT:,.0f}); "
                    f"P&L {pnl:+,.0f} before charges{ext}; next entry from {now_ist + PAUSE_AFTER_CLOSE:%H:%M:%S}"
                ))
                ctx.wake_at(now_utc + PAUSE_AFTER_CLOSE)
                return

        weekly, hedge = date.fromisoformat(position["weekly_expiry"]), hedge_of(position)
        rolls_at = roll_moment(weekly)
        if now_ist >= rolls_at:
            # version 9: the next weekly whose own roll is still to come -- never one expiring today or tomorrow
            following = next((e for e in await ctx.list_weekly_expiries(underlying.id, today, EXPIRIES_READ)
                              if e > weekly and roll_moment(e) > now_ist), None)
            if following is None:
                ctx.note("hold", reason=f"double calendar: the {weekly:%d %b} weeklies are due to roll but no later weekly is listed yet")
                ctx.wake_at(now_utc + RECHECK)
                return
            if following >= hedge:  # every second week: the next sold weekly would be the hedges' own -- everything afresh
                pnl = await close_all(HEDGE_EXIT, await forced_prices())
                ctx.state.pop("paused_until", None)
                ctx.state["reopen_due"] = True  # kept: a reopen that can't be had now is tried again, whatever the time
                rolled_note = (f"hedge roll: the next weekly, {following:%d %b}, is the bought hedges' own expiry -- all four closed, "
                               f"P&L {pnl:+,.0f} before charges; ")
            else:
                new_legs = {}
                for name in WEEKLY_LEGS:
                    leg = position["legs"][name]
                    instrument = await ctx.find_option(underlying.id, following, leg["strike"], leg["option_type"])
                    quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
                    if quote is None:
                        ctx.note("hold", reason=(
                            f"double calendar: rolling the {weekly:%d %b} weeklies to {following:%d %b} waits for a bid on the "
                            f"{leg['strike']:,.0f} {leg['option_type']}"
                        ))
                        ctx.wake_at(now_utc + RECHECK)
                        return
                    new_legs[name] = (instrument, quote[0])
                booked = dict(position.get("booked") or {"gross": 0.0, "charges": 0.0})
                closed = list(position.get("closed_legs") or [])
                rolled = []
                for name in WEEKLY_LEGS:
                    leg = position["legs"][name]
                    exit_price = await quote_or_last(leg)
                    old = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
                    await ctx.close_leg(old, "buy", leg["quantity"], exit_price)
                    booked["gross"] = booked.get("gross", 0.0) + leg_pnl(leg, exit_price)
                    booked["charges"] = booked.get("charges", 0.0) + leg["entry_charges"] + charges_of(leg, exit_price, buying=True)
                    closed.append(trade_leg(leg, exit_price))
                    instrument, price = new_legs[name]
                    await ctx.open_leg(instrument, "sell", leg["quantity"], price)
                    fresh = {**leg, "instrument_id": str(instrument.id), "expiry": following.isoformat(), "entry_price": price,
                             "entry_charges": round(charges_of(leg, price, buying=False), 2)}
                    position["entry_charges"] = round(position["entry_charges"] - leg["entry_charges"] + fresh["entry_charges"], 2)
                    position["legs"][name] = fresh
                    rolled.append(f"{leg['strike']:,.0f} {leg['option_type']} {exit_price:,.2f} -> {price:,.2f}")
                booked = {"gross": round(booked["gross"], 2), "charges": round(booked["charges"], 2)}
                position.update(booked=booked, closed_legs=closed, weekly_expiry=following.isoformat(),
                                rolls=int(position.get("rolls", 0)) + 1, rolled_at=now_utc.isoformat(),
                                booked_net=round(booked_net({"booked": booked}), 2))
                ctx.note("entered", signal="ROLL", reason=(
                    f"weekly roll {weekly:%d %b} -> {following:%d %b} at the same strikes, the {hedge:%d %b} hedges kept: "
                    f"{'; '.join(rolled)} (bought back at the ask, sold at the bid); booked so far Rs {position['booked_net']:,.0f} "
                    f"after charges; next roll {roll_moment(following):%d %b %H:%M}"
                ))
                ctx.wake_at(now_utc + RECHECK)
                return
        else:
            if not missing and ENTRY_TIME <= now_ist.time() < ENTRY_UNTIL:
                shifted = await maybe_shift(position["net_if_closed_now"])
                if shifted is not None:
                    ctx.note("entered", signal="SHIFT", reason=shifted)
                    ctx.wake_at(now_utc + RECHECK)
                    return
            if missing:
                ctx.note("hold", reason=f"double calendar open; no bid/ask for {', '.join(missing)} -- take-profit checked once all four have one")
            else:
                ctx.note("hold", reason=(
                    f"double calendar {position['strike_ce']:,.0f} CE / {position['strike_pe']:,.0f} PE "
                    f"(sold {weekly:%d %b}, bought {hedge:%d %b}): Rs {position['net_if_closed_now']:,.0f} if closed now "
                    f"at bid/ask after charges{' since the last shift' if position.get('shifts') else ''}"
                    + f"; take-profit at Rs {TAKE_PROFIT:,.0f}; "
                    f"weeklies roll {rolls_at:%d %b %H:%M}"
                    f"; {shift_text(position)}"
                ))
            ctx.wake_at(now_utc + RECHECK)
            return

    reopening = bool(ctx.state.get("reopen_due"))
    if not reopening:
        if not ENTRY_TIME <= now_ist.time() < ENTRY_UNTIL:
            ctx.note("skipped", reason=f"outside entry window ({ENTRY_TIME:%H:%M}-{ENTRY_UNTIL:%H:%M} IST)")
            return
        paused_until = ctx.state.get("paused_until")
        if paused_until and now_utc < (resume := datetime.fromisoformat(paused_until)):
            ctx.note("skipped", reason=(
                f"paused {PAUSE_AFTER_CLOSE.seconds // 60} minute{'s' if PAUSE_AFTER_CLOSE.seconds != 60 else ''} after the last close: "
            f"next entry from {resume.astimezone(IST):%H:%M:%S}"
            ))
            ctx.wake_at(resume)
            return

    spot = await ctx.get_price(underlying.id)
    if spot is None:
        ctx.note("skipped", reason="no live NIFTY spot price available")
        return
    expiries = await ctx.list_weekly_expiries(underlying.id, today, EXPIRIES_READ)
    if not expiries:
        ctx.note("skipped", reason="no live NIFTY option expiries found")
        return
    weekly = next((e for e in expiries if roll_day(e) > today), None)  # version 9: not on its roll day or after
    hedge = hedge_after(expiries, weekly) if weekly is not None else None
    if hedge is None:
        ctx.note("skipped", reason=(
            "no NIFTY weekly listed whose roll day is still to come" if weekly is None
            else f"no NIFTY expiry listed two weeks after {weekly:%d %b} yet to buy"
        ))
        if reopening:
            ctx.wake_at(now_utc + RECHECK)
        return

    async def premium_strikes(expiry: date) -> tuple[dict[str, float] | None, str, str | None]:
        """({"CE": strike, "PE": strike}, "", None) -- on each side the
        furthest OTM strike whose `expiry` bid is MIN_PREMIUM or more -- or
        (None, why not, the side -- "CE" or "PE" -- with no strike at
        MIN_PREMIUM, None when it was a strike with no book yet)."""
        strikes: dict[str, float] = {}
        for option_type in ("CE", "PE"):
            found, why, no_book = await side_strike(ctx, underlying.id, expiry, spot, option_type)
            if found is None:
                return None, why, None if no_book else option_type
            strikes[option_type] = found
        return strikes, "", None

    strikes, why, short_side = await premium_strikes(weekly)
    passed_note = ""  # version 4: the nearest weekly had no Rs 100 strike on a side, the next expiry sold instead
    if strikes is None and short_side is not None:
        later = next((e for e in expiries if e > weekly), None)
        later_hedge = hedge_after(expiries, later) if later is not None else None
        if later is None:
            why += "; no later expiry listed to sell instead"
        elif later_hedge is None:
            why += f"; no NIFTY expiry listed two weeks after {later:%d %b} yet to buy against the {later:%d %b}"
        else:
            later_strikes, later_why, _ = await premium_strikes(later)
            if later_strikes is None:
                why += f"; nor the next: {later_why}"
            else:
                passed_note = (f" (the {weekly:%d %b} weekly had no OTM {short_side} with a bid of Rs {MIN_PREMIUM:,.0f} or more: "
                               f"the next expiry instead)")
                weekly, hedge, strikes = later, later_hedge, later_strikes
    if strikes is None:
        ctx.note("skipped", reason=why)
        if reopening:
            ctx.wake_at(now_utc + RECHECK)
        return
    chosen: dict[str, tuple[Instrument, float]] = {}
    for option_type in ("CE", "PE"):
        for name, expiry, at_ask in zip(SIDE_LEGS[option_type], (weekly, hedge), (False, True)):  # the hedge at the same strike
            instrument = await ctx.find_option(underlying.id, expiry, strikes[option_type], option_type)
            quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
            if quote is None:
                ctx.note("skipped", reason=(
                    f"no {expiry:%d %b} {strikes[option_type]:,.0f} {option_type} in the catalog" if instrument is None
                    else f"no bid/ask for the {expiry:%d %b} {strikes[option_type]:,.0f} {option_type} yet"
                ))
                if reopening:
                    ctx.wake_at(now_utc + RECHECK)
                return
            chosen[name] = (instrument, quote[1] if at_ask else quote[0])

    legs, entry_charges = {}, 0.0
    for name, option_type, order_side, which in LEGS:
        instrument, price = chosen[name]
        quantity = float(LOTS_PER_LEG * (instrument.lot_size or DEFAULT_LOT_SIZE))
        await ctx.open_leg(instrument, order_side, quantity, price)
        leg = {"instrument_id": str(instrument.id), "strike": strikes[option_type], "option_type": option_type, "side": order_side,
               "expiry": (weekly if which == "weekly" else hedge).isoformat(), "quantity": quantity, "entry_price": price}
        leg["entry_charges"] = round(charges_of(leg, price, buying=order_side == "buy"), 2)
        legs[name] = leg
        entry_charges += leg["entry_charges"]

    ctx.state["position"] = {
        "regime": REGIME,
        "min_premium": MIN_PREMIUM,
        "strike_ce": strikes["CE"],
        "strike_pe": strikes["PE"],
        "hedge_rule": HEDGE_RULE,
        "entry_spot": spot,
        "weekly_expiry": weekly.isoformat(),
        "hedge_expiry": hedge.isoformat(),
        "take_profit": TAKE_PROFIT,
        "entry_charges": round(entry_charges, 2),
        "booked": {"gross": 0.0, "charges": 0.0},
        "booked_net": 0.0,
        "rolls": 0,
        "closed_legs": [],
        "legs": legs,
        "opened_at": now_utc.isoformat(),
    }
    ctx.state.pop("paused_until", None)
    ctx.state.pop("reopen_due", None)
    debit = sum(leg["entry_price"] * leg["quantity"] * (1 if leg["side"] == "buy" else -1) for leg in legs.values())
    ctx.note("entered", signal="CALENDAR", reason=(
        (rolled_note or ("reopen: " if reopening else ""))
        + f"double calendar opened, NIFTY {spot:,.1f}: {strikes['CE']:,.0f} CE (bid {chosen['short_ce'][1]:,.2f}) and "
        f"{strikes['PE']:,.0f} PE (bid {chosen['short_pe'][1]:,.2f}), the furthest OTM with a weekly bid of Rs {MIN_PREMIUM:,.0f} or more, "
        f"sell {weekly:%d %b} at the bid{passed_note}, buy {hedge:%d %b} at the same strikes at the ask "
        f"(CE {chosen['long_ce'][1]:,.2f}, PE {chosen['long_pe'][1]:,.2f}); "
        f"net debit Rs {debit:,.0f}; {LOTS_PER_LEG} lots a leg; "
        f"take-profit Rs {TAKE_PROFIT:,.0f}; weeklies roll {roll_moment(weekly):%d %b %H:%M}"
    ))
    ctx.wake_at(now_utc + RECHECK)
