"""
Conti Calendar 400 15 MIN -- a double calendar on the OTM strikes nearest a Rs 100 premium (native)
==================================================================================================

Conti Calendar 15 MIN copied with the amendments agreed 11 Nov (version 2,
11 Nov: the pause after a close cut to 1 minute; version 3, 12 Nov: the
strikes by premium, not 400 points out; version 4, 16 Nov: the next weekly
when the nearest has no Rs 100 strike): instead of one strike's straddle
calendar, a call calendar on an OTM call and a put calendar on an OTM put,
LOTS_PER_LEG (16) lots each, every fill at the order book's best price (a
sell at the bid, a buy at the ask):

  sell  the weekly CE at the call strike   buy  the monthly CE at it
  sell  the weekly PE at the put strike    buy  the monthly PE at it

The strikes (version 3; ATM+-400 in hundreds before): on each side, the
furthest OTM strike -- any STRIKE_STEP (50-point) strike, calls above NIFTY
and puts below it, however far out -- whose weekly's bid (what it sells at)
is still MIN_PREMIUM (Rs 100) or more: walked out from the first OTM strike
until a bid falls under Rs 100, so both sold premiums sit just above
Rs 100, close to each other. NIFTY 25,300 with the 25,600 CE bid 118 and
the 25,650 CE 96, the 25,000 PE 104 and the 24,950 PE 88: the 25,600 CE
and the 25,000 PE. A strike on the way with no book yet means no entry that
check (the next tries again); none at Rs 100 or more on a side, none at all.
Every new position is picked so -- the monthly roll's reopen too; a weekly
roll keeps its position's strikes.

The monthly cover (version 5, 19 Nov): each bought monthly is no longer at
its weekly's strike. On each side it is the monthly strike -- any 50-point
strike of the same type -- whose ask (what it is bought at) is between
MONTHLY_RATIO (2.5 and 3) times the sold weekly's bid; of those in range,
the one nearest 3x (the most premium, closest to the weekly strike): walked
from the weekly strike outward while the ratio is over 3, inward while
under 2.5. None in range on a side, or a strike on the way with no book: no
entry that check. Every new position, the monthly roll's reopen too; a
weekly roll keeps both its weekly and its monthly strikes. A position from
before (same-strike monthlies, no "monthly_rule") is closed at bid / ask at
the first check inside the entry window ("rule_change") and a new one opened
at once by the new rule.

Weekly: the nearest weekly expiry, next week's on an expiry day itself --
or (version 4) the expiry after it when the nearest has, on either side, no
OTM strike whose bid is still MIN_PREMIUM (the day before an expiry, its
calls or puts worth little more than their last day): both sides then sold
on that later expiry at its Rs 100 strikes. Only that one step: none there
either, no entry that check. A strike on the way with no book is no reason
to move on -- the next check tries the nearest again.
Monthly: the nearest monthly expiry after that weekly -- this month's, or
next month's in the month's last week (or when the later expiry is this
month's monthly itself).

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
               the next opens once the pause is up. No stop-loss.
  Weekly roll  at ROLL_TIME (15:00) on the sold weeklies' expiry day -- or
               at the first check after, if the app was down then -- the
               two sold weeklies are bought back at the ask and next week's
               sold at the bid at the SAME strikes; the bought monthlies
               stay. Their result is booked into the position.
  Monthly roll when next week's weekly would be the bought monthlies'
               own expiry (the month's last week), the roll instead closes
               all four ("monthly_roll") and opens a fresh double calendar
               at once -- at that moment's Rs 100 strikes, selling that week and
               buying next month's monthly -- whatever the time and pause
               (state reopen_due: a reopen that can't be had at once, a
               leg unquoted, is tried again at every check until it is).

A trade record is the whole position: every leg it held, rolled ones
included, its P&L across them, and -- the extremes -- the most profit and
the most loss "if closed now" reached while it was open (with when), also
kept on the position for the card (max_profit_reached / max_loss_reached).

Checked every RECHECK (1 second) while a position is open (ctx.wake_at),
on top of the scheduler's 10-second cycle. A leg with no bid or no ask
means no take-profit reading that check. In a backtest there is no order
book (ctx.is_backtest): the last price stands for both bid and ask.

A monthly is told by Zerodha's tradingsymbol -- NIFTY26NOV23700CE (a
month's three letters) against a weekly's NIFTY26N1723700CE. Runs through
services/paper_trading/native_runner.py.
"""

import inspect
import math
import re
import uuid
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import func, select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST
from app.services.paper_trading.trade_record import order_charges

UNDERLYING_SYMBOL = "NIFTY 50"
REGIME = "double calendar"
STRIKE_STEP = 50  # any 50-point strike (version 3; hundreds, ATM+-400, before)
MIN_PREMIUM = 100.0  # rupees: the furthest OTM strike whose sold weekly's bid is still this much (version 3)
MAX_STEPS = 40  # how many strikes out it looks on a side: 2,000 points
MONTHLY_RATIO = (2.5, 3.0)  # version 5: the bought monthly's ask over the sold weekly's bid, nearest the top
MONTHLY_RULE = "ask 2.5-3x the weekly's bid"  # kept on a position opened by version 5's rule
LOTS_PER_LEG = 16
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 30)
ENTRY_UNTIL = dtime(15, 0)
ROLL_TIME = dtime(15, 0)  # on the sold weeklies' expiry day
RECHECK = timedelta(seconds=1)  # while a position is open: how soon it's checked again
PAUSE_AFTER_CLOSE = timedelta(minutes=1)  # no new position this soon after one closes (version 2; 5 minutes in 1)
TAKE_PROFIT = 14_000.0  # rupees, the whole position after charges, at bid / ask
MONTHLY_SYMBOL = re.compile(r"^NIFTY\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d+(\.\d+)?(CE|PE)$")
LEGS = (  # name, option type, side at entry, which expiry
    ("short_ce", "CE", "sell", "weekly"),
    ("long_ce", "CE", "buy", "monthly"),
    ("short_pe", "PE", "sell", "weekly"),
    ("long_pe", "PE", "buy", "monthly"),
)
WEEKLY_LEGS = ("short_ce", "short_pe")


def first_otm(spot: float, option_type: str) -> float:
    """The nearest STRIKE_STEP strike out of the money: a call's above NIFTY, a put's below."""
    if option_type == "CE":
        return (math.floor(spot / STRIKE_STEP) + 1) * STRIKE_STEP
    return (math.ceil(spot / STRIKE_STEP) - 1) * STRIKE_STEP


def is_monthly(tradingsymbol: str | None) -> bool:
    return bool(tradingsymbol) and MONTHLY_SYMBOL.match(tradingsymbol) is not None


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


async def monthly_after(ctx, underlying_id: uuid.UUID, weekly: date) -> date | None:
    """The nearest monthly expiry after `weekly` the catalog holds."""
    rows = (
        await ctx.db.execute(
            select(Instrument.expiry, func.min(Instrument.external_ref))
            .where(Instrument.underlying_instrument_id == underlying_id, Instrument.instrument_type == "option",
                   Instrument.expiry > weekly)
            .group_by(Instrument.expiry).order_by(Instrument.expiry)
        )
    ).all()
    return next((expiry for expiry, symbol in rows if is_monthly(symbol)), None)


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

    rolled_note = ""
    if (position is not None and not force_exit and position.get("monthly_rule") != MONTHLY_RULE
            and ENTRY_TIME <= now_ist.time() < ENTRY_UNTIL):
        # version 5: a same-strike position from before closes and a new one opens at once by the new monthly rule
        pnl = await close_all("rule_change", await forced_prices())
        ctx.state.pop("paused_until", None)
        ctx.state["reopen_due"] = True
        rolled_note = f"the monthly cover rule changed (version 5): the same-strike position closed, P&L {pnl:+,.0f} before charges; "
        position = None
    if position is not None:
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
            position["net_if_closed_now"] = round(net, 2)
            position["checked_at"] = now_ist.strftime("%d %b %H:%M:%S IST")
            track_extremes(position, net, now_ist)
            if net >= TAKE_PROFIT:
                ext = extremes_text()
                pnl = await close_all("take_profit", prices)
                ctx.note("exited", signal="COVER", reason=(
                    f"take-profit: Rs {net:,.0f} after charges at bid/ask (target Rs {TAKE_PROFIT:,.0f}); "
                    f"P&L {pnl:+,.0f} before charges{ext}; next entry from {now_ist + PAUSE_AFTER_CLOSE:%H:%M:%S}"
                ))
                ctx.wake_at(now_utc + PAUSE_AFTER_CLOSE)
                return

        weekly = date.fromisoformat(position["weekly_expiry"])
        monthly = date.fromisoformat(position["monthly_expiry"])
        if weekly < today or (weekly == today and now_ist.time() >= ROLL_TIME):
            # the next weekly after today's (or, rolling late, after today): never one expiring today
            following = next((e for e in await ctx.list_weekly_expiries(underlying.id, today) if e > today), None)
            if following is None:
                ctx.note("hold", reason=f"double calendar: the {weekly:%d %b} weeklies are due to roll but no later weekly is listed yet")
                ctx.wake_at(now_utc + RECHECK)
                return
            if following >= monthly:  # the month's last week: everything rolls onto next month
                pnl = await close_all("monthly_roll", await forced_prices())
                ctx.state.pop("paused_until", None)
                ctx.state["reopen_due"] = True  # kept: a reopen that can't be had now is tried again, whatever the time
                rolled_note = f"monthly roll: all four closed, P&L {pnl:+,.0f} before charges; "
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
                    f"weekly roll {weekly:%d %b} -> {following:%d %b} at the same strikes: {'; '.join(rolled)} "
                    f"(bought back at the ask, sold at the bid); booked so far Rs {position['booked_net']:,.0f} after charges"
                ))
                ctx.wake_at(now_utc + RECHECK)
                return
        else:
            if missing:
                ctx.note("hold", reason=f"double calendar open; no bid/ask for {', '.join(missing)} -- take-profit checked once all four have one")
            else:
                cover = (f" covered by {position['monthly_strike_ce']:,.0f} CE / {position['monthly_strike_pe']:,.0f} PE"
                         if position.get("monthly_strike_ce") is not None else "")
                ctx.note("hold", reason=(
                    f"double calendar {position['strike_ce']:,.0f} CE / {position['strike_pe']:,.0f} PE{cover} "
                    f"(sold {weekly:%d %b}, bought {monthly:%d %b}): Rs {position['net_if_closed_now']:,.0f} if closed now "
                    f"at bid/ask after charges; take-profit at Rs {TAKE_PROFIT:,.0f}; weeklies roll {weekly:%d %b} {ROLL_TIME:%H:%M}"
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
    expiries = await ctx.list_weekly_expiries(underlying.id, today)
    if not expiries:
        ctx.note("skipped", reason="no live NIFTY option expiries found")
        return
    weekly = expiries[1] if today == expiries[0] and len(expiries) > 1 else expiries[0]
    if weekly == today:
        ctx.note("skipped", reason="only today's expiry is listed -- nothing to sell for a later week")
        return
    monthly = await monthly_after(ctx, underlying.id, weekly)
    if monthly is None:
        ctx.note("skipped", reason=f"no monthly NIFTY expiry after {weekly:%d %b} in the catalog yet")
        return

    async def premium_strikes(expiry: date) -> tuple[dict[str, float] | None, str, str | None]:
        """({"CE": strike, "PE": strike}, "", None) -- on each side the
        furthest OTM strike whose `expiry` bid is MIN_PREMIUM or more -- or
        (None, why not, the side -- "CE" or "PE" -- with no strike at
        MIN_PREMIUM, None when it was a strike with no book yet)."""
        strikes: dict[str, float] = {}
        for option_type in ("CE", "PE"):
            found, step = None, STRIKE_STEP if option_type == "CE" else -STRIKE_STEP
            for k in range(MAX_STEPS):
                strike = first_otm(spot, option_type) + k * step
                instrument = await ctx.find_option(underlying.id, expiry, strike, option_type)
                if instrument is None:
                    break  # past the catalog's last strike
                quote = await bid_ask(ctx, instrument.id)
                if quote is None:
                    return None, f"no bid/ask for the {expiry:%d %b} {strike:,.0f} {option_type} yet", None
                if quote[0] < MIN_PREMIUM:
                    break
                found = strike
            if found is None:
                return None, f"no OTM {expiry:%d %b} {option_type} with a bid of Rs {MIN_PREMIUM:,.0f} or more", option_type
            strikes[option_type] = found
        return strikes, "", None

    strikes, why, short_side = await premium_strikes(weekly)
    passed_note = ""  # version 4: the nearest weekly had no Rs 100 strike on a side, the next expiry sold instead
    if strikes is None and short_side is not None:
        later = next((e for e in expiries if e > weekly), None)
        later_monthly = await monthly_after(ctx, underlying.id, later) if later is not None else None
        if later is None:
            why += "; no later expiry listed to sell instead"
        elif later_monthly is None:
            why += f"; no monthly NIFTY expiry after {later:%d %b} in the catalog yet to buy against the {later:%d %b}"
        else:
            later_strikes, later_why, _ = await premium_strikes(later)
            if later_strikes is None:
                why += f"; nor the next: {later_why}"
            else:
                passed_note = (f" (the {weekly:%d %b} weekly had no OTM {short_side} with a bid of Rs {MIN_PREMIUM:,.0f} or more: "
                               f"the next expiry instead)")
                weekly, monthly, strikes = later, later_monthly, later_strikes
    if strikes is None:
        ctx.note("skipped", reason=why)
        if reopening:
            ctx.wake_at(now_utc + RECHECK)
        return
    async def monthly_cover(option_type: str, weekly_strike: float, weekly_bid: float) -> tuple[tuple | None, str]:
        """((the monthly instrument, its ask, ask / weekly bid, its strike), "") --
        the strike whose ask is MONTHLY_RATIO times `weekly_bid`, nearest the
        top -- or (None, why not). The premium falls moving OTM, so it walks
        OTM from the weekly strike while the ratio is over the range, ITM
        while it's under (and on through the range for the highest in it)."""
        lo, hi = MONTHLY_RATIO
        otm = STRIKE_STEP if option_type == "CE" else -STRIKE_STEP
        seen: dict[float, tuple | str | None] = {}

        async def at(strike: float):
            if strike not in seen:
                instrument = await ctx.find_option(underlying.id, monthly, strike, option_type)
                quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
                seen[strike] = None if instrument is None else ("no book" if quote is None else (instrument, quote[1], quote[1] / weekly_bid))
            return seen[strike]

        k = weekly_strike
        r = await at(k)
        if r is None:
            return None, f"no {monthly:%d %b} {k:,.0f} {option_type} in the catalog"
        if r == "no book":
            return None, f"no bid/ask for the {monthly:%d %b} {k:,.0f} {option_type} yet"
        direction = otm if r[2] > hi else -otm
        best = (k, r) if lo <= r[2] <= hi else None
        for _ in range(MAX_STEPS):
            if best is not None and direction == otm:
                break  # walking OTM, the first in range is the nearest 3x
            nk = k + direction
            nr = await at(nk)
            if nr is None:
                break  # past the catalog's last strike
            if nr == "no book":
                return None, f"no bid/ask for the {monthly:%d %b} {nk:,.0f} {option_type} yet"
            if lo <= nr[2] <= hi and best is not None and nr[2] <= best[1][2]:
                break  # no more premium a strike further: the one before stands
            k, r = nk, nr
            if lo <= r[2] <= hi:
                best = (k, r)
            elif best is not None or (direction == otm and r[2] < lo) or (direction != otm and r[2] > hi):
                break  # past the range (or jumped over it)
        if best is None:
            return None, (f"no {monthly:%d %b} {option_type} with an ask {lo:g}-{hi:g}x the {weekly:%d %b} "
                          f"{weekly_strike:,.0f} {option_type}'s bid of {weekly_bid:,.2f}")
        return (best[1][0], best[1][1], best[1][2], best[0]), ""

    chosen: dict[str, tuple[Instrument, float]] = {}
    leg_strikes: dict[str, float] = {}
    ratios: dict[str, float] = {}
    for option_type in ("CE", "PE"):
        weekly_name = next(n for n, t, _, w in LEGS if t == option_type and w == "weekly")
        monthly_name = next(n for n, t, _, w in LEGS if t == option_type and w == "monthly")
        instrument = await ctx.find_option(underlying.id, weekly, strikes[option_type], option_type)
        quote = await bid_ask(ctx, instrument.id) if instrument is not None else None
        why = (f"no {weekly:%d %b} {strikes[option_type]:,.0f} {option_type} in the catalog" if instrument is None
               else f"no bid/ask for the {weekly:%d %b} {strikes[option_type]:,.0f} {option_type} yet" if quote is None else "")
        cover = None
        if not why:
            chosen[weekly_name], leg_strikes[weekly_name] = (instrument, quote[0]), strikes[option_type]
            cover, why = await monthly_cover(option_type, strikes[option_type], quote[0])
        if cover is None:
            ctx.note("skipped", reason=why)
            if reopening:
                ctx.wake_at(now_utc + RECHECK)
            return
        m_instrument, m_ask, ratio, m_strike = cover
        chosen[monthly_name], leg_strikes[monthly_name], ratios[option_type] = (m_instrument, m_ask), m_strike, round(ratio, 2)

    legs, entry_charges = {}, 0.0
    for name, option_type, order_side, which in LEGS:
        instrument, price = chosen[name]
        quantity = float(LOTS_PER_LEG * (instrument.lot_size or DEFAULT_LOT_SIZE))
        await ctx.open_leg(instrument, order_side, quantity, price)
        leg = {"instrument_id": str(instrument.id), "strike": leg_strikes[name], "option_type": option_type, "side": order_side,
               "expiry": (weekly if which == "weekly" else monthly).isoformat(), "quantity": quantity, "entry_price": price}
        leg["entry_charges"] = round(charges_of(leg, price, buying=order_side == "buy"), 2)
        legs[name] = leg
        entry_charges += leg["entry_charges"]

    ctx.state["position"] = {
        "regime": REGIME,
        "min_premium": MIN_PREMIUM,
        "strike_ce": strikes["CE"],
        "strike_pe": strikes["PE"],
        "monthly_strike_ce": leg_strikes["long_ce"],
        "monthly_strike_pe": leg_strikes["long_pe"],
        "monthly_ratio_ce": ratios["CE"],
        "monthly_ratio_pe": ratios["PE"],
        "monthly_rule": MONTHLY_RULE,
        "entry_spot": spot,
        "weekly_expiry": weekly.isoformat(),
        "monthly_expiry": monthly.isoformat(),
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
        (rolled_note or ("monthly roll's reopen: " if reopening else ""))
        + f"double calendar opened, NIFTY {spot:,.1f}: {strikes['CE']:,.0f} CE (bid {chosen['short_ce'][1]:,.2f}) and "
        f"{strikes['PE']:,.0f} PE (bid {chosen['short_pe'][1]:,.2f}), the furthest OTM with a weekly bid of Rs {MIN_PREMIUM:,.0f} or more, "
        f"sell {weekly:%d %b} at the bid{passed_note}, buy {monthly:%d %b} {leg_strikes['long_ce']:,.0f} CE (ask {chosen['long_ce'][1]:,.2f}, "
        f"{ratios['CE']:g}x) and {leg_strikes['long_pe']:,.0f} PE (ask {chosen['long_pe'][1]:,.2f}, {ratios['PE']:g}x) at the ask; "
        f"net debit Rs {debit:,.0f}; {LOTS_PER_LEG} lots a leg; "
        f"take-profit Rs {TAKE_PROFIT:,.0f}"
    ))
    ctx.wake_at(now_utc + RECHECK)
