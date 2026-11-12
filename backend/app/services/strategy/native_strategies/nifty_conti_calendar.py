"""
Conti Calendar 15 MIN -- Conti Straddle's short straddle with the same strike bought monthly (native)
====================================================================================================

Conti Straddle 15 MIN with a monthly hedge (agreed 4 Nov; version 2, 5 Nov:
take-profit Rs 14,000 and a 15:00 close every day; version 3, 6 Nov:
strikes in hundreds only; version 4, 6 Nov: version 3's limit of two trades
a day taken off again; version 5, 6 Nov: re-checked every second while a
position is open; version 6, 6 Nov: version 3's hundreds-only strikes taken
off again; version 7, 6 Nov: a 5-minute pause after every close; version 8,
8 Nov: the daily close moved to 15:29; version 9, 9 Nov: no new position
after one closes from 14:00; version 10, 11 Nov: the pause after a close
cut to 1 minute; version 11, 12 Nov: a losing position carried past 15:29,
rolled at its weekly's expiry): four legs at one
strike, LOTS_PER_LEG (16) lots each --

  sell  the weekly CE and PE   the nearest weekly expiry; next week's on an
                               expiry day itself (as Conti)
  buy   the monthly CE and PE  the nearest monthly expiry after that weekly:
                               this month's, or next month's in the month's
                               last week, when this month's IS the weekly

-- opened from 09:30 (ENTRY_TIME) to 15:00 IST whenever none is open, as
many a day as that allows, but never within PAUSE_AFTER_CLOSE (1 minute,
version 10; 5 minutes in versions 7-9) of the last one closing -- by
take-profit or a manual exit: the next opens as soon as that minute is up,
at that moment's PCR, strike and bid/ask (closed 11:27:15, next from
11:28:15). The pause is kept in the
strategy's state (paused_until), so a restart doesn't cut it short. And
once one closes at or after DAY_DONE_AFTER (14:00; version 9), by
take-profit or a manual exit, none opens again that day: a position taken
before then that hasn't reached the take-profit is simply held to the
close. A close before 14:00 still allows the next after its pause (closed
13:58:30, next at 13:59:30), up to 15:00. The last close is kept in the state
(last_closed_at). The PCR picks the strike as Conti picks it, from
the 50-point strikes (STRIKE_MULTIPLE) around NIFTY by the weekly's live
premiums:

  PCR below 1     the strike whose CALL premium is just above its PUT's
  PCR 1 or above  the strike whose PUT premium is just above its CALL's

Nearer NIFTY on a tie; the strikes looked at are the one nearest NIFTY and
CANDIDATE_STEPS either side.

At bid and ask: every fill is at the order book's best price -- a sell at
the bid, a buy at the ask -- and the take-profit reads what closing all four
right now would really give: the sold legs bought back at the ask, the
bought legs sold at the bid, less the estimated charges of all eight orders
(trade_record.order_charges). At TAKE_PROFIT (Rs 14,000; Rs 50,000 in
version 1) or more, all four close at those prices. Checked every RECHECK
(1 second) while a position is open and NSE trades -- the strategy asks to
be run again then (ctx.wake_at) on top of the scheduler's 10-second cycle --
and is run again right when a pause ends; a leg with no bid or no ask waits
for one.

The 15:29 close (version 11, 12 Nov; before it every position closed at
DAILY_CLOSE, and one left from an earlier day at its first check): when
the take-profit hasn't come by DAILY_CLOSE, a position IN PROFIT ("Net if
closed now" at bid / ask after charges, Rs 0 or more) closes then at bid /
ask; one IN LOSS is carried overnight -- the take-profit still checked
every second -- and each later day's 15:29 decides the same way. Not 15:30
itself: NSE shuts then and the app runs no strategy after 15:30:00. With a
leg unquoted at 15:29 profit or loss can't be told: it's held and checked
again. New positions open only until ENTRY_UNTIL (15:00).

The expiry roll (version 11): a carried position is never held past its
weekly's expiry. At ROLL_TIME (15:00) on that day -- or the first check
after, the app down then -- it closes: IN LOSS ("expiry_roll") a new
position opens at once by the usual rule (the PCR's strike near NIFTY now,
on next week's weekly and its monthly), whatever the time, the pause and
the 14:00 rule (state reopen_due, that day only); IN PROFIT
("weekly_expiry_3pm") it just closes, nothing new that day. No stop-loss.

A monthly is told by Zerodha's tradingsymbol -- NIFTY26NOV23700CE (a month's
three letters) against a weekly's NIFTY26N1723700CE. In a backtest there is
no order book (ctx.is_backtest): the last price stands for both bid and ask.

PCR is ctx.get_pcr(timeframe="15m"). Runs through
services/paper_trading/native_runner.py.
"""

import re
import uuid
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import func, select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST
from app.services.paper_trading.trade_record import order_charges

UNDERLYING_SYMBOL = "NIFTY 50"
PCR_TIMEFRAME = "15m"
STRIKE_MULTIPLE = 50  # every 50-point strike, as Conti (version 6; versions 3-5 took hundreds only)
CANDIDATE_STEPS = 3  # strikes looked at either side of the one nearest NIFTY
LOTS_PER_LEG = 16
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 30)
ENTRY_UNTIL = dtime(15, 0)
DAILY_CLOSE = dtime(15, 29)  # every day, whatever's open closes at this time (version 8; 15:00 before)
DAY_DONE_AFTER = dtime(14, 0)  # a close at or after this ends the day's new positions (version 9)
ROLL_TIME = dtime(15, 0)  # on a carried position's weekly expiry day: closed, and in loss rolled into a new one
DAILY_CLOSE_REASON = "time_cutoff_329pm"  # its trade's exit_reason ("time_cutoff_3pm" for the 15:00 close)
RECHECK = timedelta(seconds=1)  # while a position is open: how soon it's checked again (version 5)
PAUSE_AFTER_CLOSE = timedelta(minutes=1)  # no new position this soon after one closes (version 10; 5 minutes in 7-9)
TAKE_PROFIT = 14_000.0  # rupees, after charges, at bid / ask
PCR_PIVOT = 1.0
MONTHLY_SYMBOL = re.compile(r"^NIFTY\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d+(\.\d+)?(CE|PE)$")
LEGS = (  # name, option type, side at entry, which expiry
    ("short_ce", "CE", "sell", "weekly"),
    ("short_pe", "PE", "sell", "weekly"),
    ("long_ce", "CE", "buy", "monthly"),
    ("long_pe", "PE", "buy", "monthly"),
)


def side_for(pcr: float) -> str:
    """"call": the CE premium just above the PE; "put": the PE just above the CE."""
    return "call" if pcr < PCR_PIVOT else "put"


def candidate_strikes(spot: float) -> list[float]:
    nearest = round(spot / STRIKE_MULTIPLE) * STRIKE_MULTIPLE
    return [nearest + STRIKE_MULTIPLE * k for k in range(-CANDIDATE_STEPS, CANDIDATE_STEPS + 1)]


def pick_strike(quotes: dict[float, tuple[float, float]], side: str, spot: float) -> float | None:
    """`quotes`: strike -> (CE premium, PE premium). The strike where that
    side's premium leads the other by the least (and does lead); the
    nearer NIFTY on a tie; None when none leads."""
    best = None
    for strike, (ce, pe) in quotes.items():
        lead = ce - pe if side == "call" else pe - ce
        if lead <= 0:
            continue
        key = (lead, abs(strike - spot))
        if best is None or key < best[0]:
            best = (key, strike)
    return best[1] if best is not None else None


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


def net_at(position: dict, prices: dict[str, float]) -> float:
    """The position's profit after every charge were each leg closed at `prices` (name -> price)."""
    return (sum(leg_pnl(leg, prices[name]) for name, leg in position["legs"].items()) - position["entry_charges"]
            - sum(charges_of(leg, prices[name], buying=leg["side"] == "sell") for name, leg in position["legs"].items()))


def if_closed_now(position: dict, quotes: dict[str, tuple[float, float]]) -> tuple[float, float, dict[str, float]]:
    """(profit after every charge, the closing orders' charges, each leg's
    closing price) were all four closed now at `quotes` (name -> (bid, ask))."""
    gross, exit_charges, prices = 0.0, 0.0, {}
    for name, leg in position["legs"].items():
        bid, ask = quotes[name]
        price = closing_price(leg, bid, ask)
        prices[name] = price
        gross += leg_pnl(leg, price)
        exit_charges += charges_of(leg, price, buying=leg["side"] == "sell")
    return gross - position["entry_charges"] - exit_charges, exit_charges, prices


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
    rolled_note = ""

    async def close_all(exit_reason: str, prices: dict[str, float]) -> float:
        trade_legs, total = [], 0.0
        for name, leg in position["legs"].items():
            instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
            price = prices[name]
            await ctx.close_leg(instrument, "buy" if leg["side"] == "sell" else "sell", leg["quantity"], price)
            total += leg_pnl(leg, price)
            trade_legs.append({
                "instrument_id": leg["instrument_id"], "side": "short" if leg["side"] == "sell" else "long",
                "quantity": leg["quantity"], "entry_price": leg["entry_price"], "exit_price": price,
            })
        outlay = sum(leg["entry_price"] * leg["quantity"] for leg in position["legs"].values())
        await ctx.record_trade(
            legs=trade_legs, pnl=total, pnl_pct=(total / outlay * 100) if outlay else 0.0, exit_reason=exit_reason,
            opened_at=datetime.fromisoformat(position["opened_at"]),
        )
        ctx.state["position"] = None
        ctx.state["paused_until"] = (now_utc + PAUSE_AFTER_CLOSE).isoformat()
        ctx.state["last_closed_at"] = now_utc.isoformat()
        return total

    def after_close() -> str:
        """What follows a close made now, for its note -- and its wake-up."""
        if now_ist.time() >= DAY_DONE_AFTER:
            return f"no new trade today (closed after {DAY_DONE_AFTER:%H:%M})"
        ctx.wake_at(now_utc + PAUSE_AFTER_CLOSE)
        return f"next entry from {now_ist + PAUSE_AFTER_CLOSE:%H:%M:%S}"

    async def forced_prices() -> dict[str, float]:
        """Closing prices that must be had now: bid / ask, else the last
        price, else (an expired contract nothing prices) the entry."""
        prices = {}
        for name, leg in position["legs"].items():
            iid = uuid.UUID(leg["instrument_id"])
            quote = await bid_ask(ctx, iid)
            if quote is not None:
                prices[name] = closing_price(leg, *quote)
                continue
            last = await ctx.get_price(iid)
            prices[name] = last if last is not None else leg["entry_price"]
        return prices

    if position is not None:
        weekly = date.fromisoformat(position["weekly_expiry"])
        if force_exit:
            await close_all("manual", await forced_prices())
            ctx.note("exited", signal="COVER", reason=f"manual exit; {after_close()}")
            return
        quotes, missing = {}, []
        for name, leg in position["legs"].items():
            quote = await bid_ask(ctx, uuid.UUID(leg["instrument_id"]))
            if quote is None:
                missing.append(name)
            else:
                quotes[name] = quote
        net = prices = None
        if not missing:
            net, exit_charges, prices = if_closed_now(position, quotes)
            position["net_if_closed_now"] = round(net, 2)
            position["checked_at"] = now_ist.strftime("%d %b %H:%M:%S IST")
            if net >= TAKE_PROFIT:
                pnl = await close_all("take_profit", prices)
                ctx.note("exited", signal="COVER", reason=(
                    f"take-profit: Rs {net:,.0f} after charges at bid/ask (target Rs {TAKE_PROFIT:,.0f}); "
                    f"P&L {pnl:+,.0f} before charges; {after_close()}"  # wakes for the next one once the pause is up
                ))
                return
        monthly = date.fromisoformat(position["monthly_expiry"])
        if weekly < today or (weekly == today and now_ist.time() >= ROLL_TIME):
            # its weekly expires today (or has, the app down at ROLL_TIME): never held past it
            if missing and weekly == today and now_ist.time() < DAILY_CLOSE:
                ctx.note("hold", reason=(
                    f"calendar {position['strike']:,.0f}: its {weekly:%d %b} weekly expires today; no bid/ask for "
                    f"{', '.join(missing)} -- closed once all four have one"
                ))
                ctx.wake_at(now_utc + RECHECK)
                return
            if prices is None:
                prices = await forced_prices()
                net = net_at(position, prices)
            if net < 0:  # in loss: closed, and a new one opened at once at the strike now
                pnl = await close_all("expiry_roll", prices)
                ctx.state.pop("paused_until", None)
                ctx.state["reopen_due"] = today.isoformat()
                rolled_note = (f"expiry roll: the {weekly:%d %b} weekly expiring with Rs {net:,.0f} in loss -- all four closed, "
                               f"P&L {pnl:+,.0f} before charges; ")
            else:  # in profit: closed, nothing new that day
                pnl = await close_all("weekly_expiry_3pm", prices)
                ctx.note("exited", signal="COVER", reason=(
                    f"the {weekly:%d %b} weekly expiring, in profit (Rs {net:,.0f} after charges): all four closed at bid/ask, "
                    f"P&L {pnl:+,.0f} before charges"
                ))
                return
        elif now_ist.time() >= DAILY_CLOSE:  # version 11: in profit it closes, in loss it's carried overnight
            if net is None:
                ctx.note("hold", reason=(
                    f"calendar {position['strike']:,.0f}: no bid/ask for {', '.join(missing)} at the {DAILY_CLOSE:%H:%M} close -- "
                    f"profit or loss unknown, held and checked again"
                ))
                ctx.wake_at(now_utc + RECHECK)
                return
            if net >= 0:
                pnl = await close_all(DAILY_CLOSE_REASON, prices)
                ctx.note("exited", signal="COVER", reason=(
                    f"{DAILY_CLOSE:%H:%M} close in profit (Rs {net:,.0f} after charges): all four legs closed at bid/ask, "
                    f"P&L {pnl:+,.0f} before charges"
                ))
                return
            position["carried_from"] = position.get("carried_from") or today.isoformat()
            ctx.note("hold", reason=(
                f"calendar {position['strike']:,.0f}: Rs {net:,.0f} in loss at {DAILY_CLOSE:%H:%M} -- carried overnight; "
                f"take-profit at Rs {TAKE_PROFIT:,.0f}; closes at a later {DAILY_CLOSE:%H:%M} in profit"
            ))
            ctx.wake_at(now_utc + RECHECK)
            return
        else:
            if missing:
                ctx.note("hold", reason=(
                    f"calendar {position['strike']:,.0f} open; no bid/ask for {', '.join(missing)} -- "
                    f"take-profit checked once all four have one"
                ))
            else:
                ctx.note("hold", reason=(
                    f"calendar {position['strike']:,.0f} (sold {weekly:%d %b}, bought {monthly:%d %b}): "
                    f"Rs {net:,.0f} if closed now at bid/ask after charges; take-profit at Rs {TAKE_PROFIT:,.0f}; "
                    f"at {DAILY_CLOSE:%H:%M} closed in profit, carried in loss"
                ))
            ctx.wake_at(now_utc + RECHECK)
            return

    reopen = ctx.state.get("reopen_due")
    reopening = reopen == today.isoformat()  # an expiry roll's new position: whatever the time, the pause, the 14:00 rule
    if reopen and not reopening:
        ctx.state.pop("reopen_due")  # one not had that day: the next opens by the usual rules
    if reopening:
        ctx.wake_at(now_utc + RECHECK)  # tried again a second on until it's had
    else:
        if not ENTRY_TIME <= now_ist.time() < ENTRY_UNTIL:
            ctx.note("skipped", reason=f"outside entry window ({ENTRY_TIME:%H:%M}-{ENTRY_UNTIL:%H:%M} IST)")
            return
        last_closed = ctx.state.get("last_closed_at")
        closed_ist = datetime.fromisoformat(last_closed).astimezone(IST) if last_closed else None
        if closed_ist is not None and closed_ist.date() == today and closed_ist.time() >= DAY_DONE_AFTER:
            ctx.note("skipped", reason=(
                f"no new trade today: the last closed at {closed_ist:%H:%M:%S}, after {DAY_DONE_AFTER:%H:%M}; next from {ENTRY_TIME:%H:%M} tomorrow"
            ))
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
    pcr = await ctx.get_pcr(timeframe=PCR_TIMEFRAME)
    if pcr is None:
        ctx.note("skipped", reason="no PCR data available yet")
        return
    side = side_for(pcr)

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

    contracts: dict[float, dict[str, tuple[Instrument, float]]] = {}
    for strike in candidate_strikes(spot):
        pair = {}
        for option_type in ("CE", "PE"):
            instrument = await ctx.find_option(underlying.id, weekly, strike, option_type)
            price = await ctx.get_price(instrument.id) if instrument is not None else None
            if price is None:
                break
            pair[option_type] = (instrument, price)
        if len(pair) == 2:
            contracts[strike] = pair
    strike = pick_strike({k: (pair["CE"][1], pair["PE"][1]) for k, pair in contracts.items()}, side, spot)
    if strike is None:
        heavier = "CALL" if side == "call" else "PUT"
        ctx.note("skipped", reason=(
            f"PCR {pcr:.3f}: no strike within {STRIKE_MULTIPLE * CANDIDATE_STEPS} of NIFTY with the {heavier} premium above "
            f"the other ({len(contracts)} with both premiums live)"
        ))
        return

    chosen: dict[str, tuple[Instrument, float, str]] = {}
    for name, option_type, order_side, which in LEGS:
        instrument = (contracts[strike][option_type][0] if which == "weekly"
                      else await ctx.find_option(underlying.id, monthly, strike, option_type))
        if instrument is None:
            ctx.note("skipped", reason=f"no {monthly:%d %b} monthly {strike:,.0f} {option_type} in the catalog yet")
            return
        quote = await bid_ask(ctx, instrument.id)
        if quote is None:
            ctx.note("skipped", reason=f"no bid/ask for the {strike:,.0f} {option_type} {'weekly' if which == 'weekly' else 'monthly'} yet")
            return
        bid, ask = quote
        chosen[name] = (instrument, bid if order_side == "sell" else ask, order_side)

    legs, entry_charges = {}, 0.0
    for name, option_type, order_side, which in LEGS:
        instrument, price, _ = chosen[name]
        quantity = float(LOTS_PER_LEG * (instrument.lot_size or DEFAULT_LOT_SIZE))
        await ctx.open_leg(instrument, order_side, quantity, price)
        legs[name] = {"instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": order_side,
                      "expiry": (weekly if which == "weekly" else monthly).isoformat(), "quantity": quantity, "entry_price": price}
        entry_charges += charges_of(legs[name], price, buying=order_side == "buy")

    ctx.state.pop("reopen_due", None)
    ctx.state["position"] = {
        "regime": "calendar",
        "side": side,
        "strike": strike,
        "pcr_at_entry": pcr,
        "entry_spot": spot,
        "weekly_expiry": weekly.isoformat(),
        "monthly_expiry": monthly.isoformat(),
        "take_profit": TAKE_PROFIT,
        "entry_charges": round(entry_charges, 2),
        "legs": legs,
        "opened_at": now_utc.isoformat(),
    }
    ctx.state.pop("trades_today", None)  # version 3's count, no longer kept
    ctx.state.pop("paused_until", None)
    sold = legs["short_ce"]["entry_price"] + legs["short_pe"]["entry_price"]
    bought = legs["long_ce"]["entry_price"] + legs["long_pe"]["entry_price"]
    ctx.note("entered", signal="CALENDAR", reason=(
        rolled_note + f"{side}-heavy calendar opened at {strike:,.0f} (PCR {pcr:.3f}): sell {weekly:%d %b} CE+PE {sold:,.2f} at the bid, "
        f"buy {monthly:%d %b} CE+PE {bought:,.2f} at the ask; {LOTS_PER_LEG} lots a leg; take-profit Rs {TAKE_PROFIT:,.0f}"
    ))
    ctx.wake_at(now_utc + RECHECK)
