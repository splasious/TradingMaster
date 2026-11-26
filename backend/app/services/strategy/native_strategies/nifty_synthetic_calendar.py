"""
Synthetic Calendar 15 MIN -- this week's synthetic long against the weekly two weeks out's synthetic short, one strike (native)
=============================================================================================================================

Agreed 20 Nov from a position the user built (NIFTY 25,711.95: buy 23 Nov
25,700 CE, sell 23 Nov 25,700 PE, buy Dec 25,850 PE, sell Dec 25,850 CE, 20
lots each), with all four legs at one strike and (version 1) the month's monthly: at
the STRIKE_STEP (50-point) strike nearest NIFTY, LOTS_PER_LEG (20) lots a
leg, every fill at the order book's best price (a sell at the bid, a buy at
the ask):

  weekly   buy  the CE   sell the PE    a synthetic long NIFTY to the weekly
  hedge    buy  the PE   sell the CE    a synthetic short NIFTY to the hedge's expiry

LEGS below. The two synthetics move with NIFTY in opposite directions and a
CE and PE at one strike carry no volatility between them, so the position
lives on the gap between the weekly's and the hedge's forward prices.
Synthetic Calendar Reverse 15 MIN (nifty_synthetic_calendar_reverse.py) is
the same with every leg the other way.

Weekly: the nearest weekly expiry, next week's on an expiry day itself.
Hedge (version 2, 26 Nov; the nearest monthly after the weekly before): the
weekly two weeks after it -- the listed expiry HEDGE_DAYS (11 to 17) days
after it, nearest 14, so an expiry a holiday brings forward still counts --
at the same strike; e.g. this week's 1 Dec against the 15 Dec. None listed
means no entry that check. A position from before (a monthly hedge: no
"hedge_rule") is closed at bid / ask at the first check from ENTRY_TIME to
ENTRY_UNTIL ("rule_change") and a new one opened at once by the new rule.

Opened from ENTRY_TIME (09:30) until ENTRY_UNTIL (15:00) whenever none is
open, never within PAUSE_AFTER_CLOSE (1 minute) of the last one closing
(kept in the state, paused_until, so a restart doesn't cut it short). Every
leg must have a bid and an ask; one without means no entry that check.

Held overnight until:

  Take-profit  "Net if closed now" -- the sold legs bought back at the ask,
               the bought legs sold at the bid, less every order's charges
               (trade_record.order_charges) -- TAKE_PROFIT (Rs 14,000) or
               more: all four close at those prices; the next opens once
               the pause is up. Read every RECHECK (1 second).
  The weekly   at WEEKLY_CLOSE (15:00) on the weekly's expiry day -- or at
               the first check after, the app down then -- all four close
               ("weekly_expiry_3pm"), in profit or not; nothing new that day
               (the entry window ends at 15:00).

No stop-loss. Paper only, no Telegram alerts. In a backtest there is no
order book (ctx.is_backtest): the last price stands for both bid and ask.
Runs through services/paper_trading/native_runner.py.
"""

import uuid
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST
from app.services.paper_trading.trade_record import order_charges

NAME = "Synthetic Calendar 15 MIN"
UNDERLYING_SYMBOL = "NIFTY 50"
REGIME = "synthetic calendar"
BIAS = "weekly synthetic long, hedge synthetic short"
STRIKE_STEP = 50
LOTS_PER_LEG = 20
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 30)
ENTRY_UNTIL = dtime(15, 0)
WEEKLY_CLOSE = dtime(15, 0)  # on the weekly's expiry day: all four close
RECHECK = timedelta(seconds=1)  # while a position is open: how soon it's checked again
PAUSE_AFTER_CLOSE = timedelta(minutes=1)  # no new position this soon after one closes
TAKE_PROFIT = 14_000.0  # rupees, after charges, at bid / ask
HEDGE_DAYS = (11, 17)  # version 2: the hedge's expiry this many days after the weekly's, nearest 14
HEDGE_RULE = "the weekly two weeks out, same strike"  # kept on a position opened by version 2's rule
EXPIRIES_READ = 8  # listed expiries read: the weekly's and two weeks past it
LEGS = (  # name, option type, side at entry, which expiry
    ("weekly_ce", "CE", "buy", "weekly"),
    ("weekly_pe", "PE", "sell", "weekly"),
    ("hedge_pe", "PE", "buy", "hedge"),
    ("hedge_ce", "CE", "sell", "hedge"),
)


def atm_strike(spot: float) -> float:
    """The STRIKE_STEP strike nearest NIFTY (a half-way NIFTY rounds up)."""
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def hedge_after(expiries: list[date], weekly: date) -> date | None:
    """Version 2: the listed expiry two weeks after `weekly` -- HEDGE_DAYS (11 to 17) days on, nearest 14 -- or None."""
    lo, hi = HEDGE_DAYS
    near = [e for e in expiries if lo <= (e - weekly).days <= hi]
    return min(near, key=lambda e: abs((e - weekly).days - 14)) if near else None


def hedge_of(position: dict) -> date:
    """The hedge legs' expiry (a position from before version 2: its monthly's)."""
    return date.fromisoformat(position.get("hedge_expiry") or position["monthly_expiry"])


def closing_price(leg: dict, bid: float, ask: float) -> float:
    """What closing `leg` fills at: a sold leg is bought back at the ask, a bought one sold at the bid."""
    return ask if leg["side"] == "sell" else bid


def leg_pnl(leg: dict, exit_price: float) -> float:
    diff = leg["entry_price"] - exit_price if leg["side"] == "sell" else exit_price - leg["entry_price"]
    return diff * leg["quantity"]


def charges_of(leg: dict, price: float, buying: bool) -> float:
    return order_charges({"option_type": leg["option_type"], "quantity": leg["quantity"]}, price, buying, False) or 0.0


def if_closed_now(position: dict, quotes: dict[str, tuple[float, float]]) -> tuple[float, dict[str, float]]:
    """(the profit after every charge, each leg's closing price) were all four closed now at `quotes` (name -> (bid, ask))."""
    gross, exit_charges, prices = 0.0, 0.0, {}
    for name, leg in position["legs"].items():
        bid, ask = quotes[name]
        price = closing_price(leg, bid, ask)
        prices[name] = price
        gross += leg_pnl(leg, price)
        exit_charges += charges_of(leg, price, buying=leg["side"] == "sell")
    return gross - position["entry_charges"] - exit_charges, prices


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

    underlying = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))
    ).scalar_one_or_none()
    if underlying is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return

    force_exit = ctx.state.pop("force_exit", False)
    position = ctx.state.get("position")

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
            opened_at=datetime.fromisoformat(position["opened_at"]), closed_at=now_utc,
        )
        ctx.state["position"] = None
        ctx.state["paused_until"] = (now_utc + PAUSE_AFTER_CLOSE).isoformat()
        return total

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

    rule_note = ""
    if (position is not None and not force_exit and position.get("hedge_rule") != HEDGE_RULE
            and ENTRY_TIME <= now_ist.time() < ENTRY_UNTIL):
        # version 2: a position from before -- a monthly hedge -- closes and a new one opens at once by the new rule
        pnl = await close_all("rule_change", await forced_prices())
        ctx.state.pop("paused_until", None)
        rule_note = (f"the hedge rule changed (version 2: the weekly two weeks out at the same strike, not a monthly): "
                     f"the position closed, P&L {pnl:+,.0f} before charges; ")
        position = None
    if position is not None:
        weekly = date.fromisoformat(position["weekly_expiry"])
        if force_exit:
            pnl = await close_all("manual", await forced_prices())
            ctx.note("exited", signal="COVER", reason=(
                f"manual exit; P&L {pnl:+,.0f} before charges; next entry from {now_ist + PAUSE_AFTER_CLOSE:%H:%M:%S}"
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
        net = prices = None
        if not missing:
            net, prices = if_closed_now(position, quotes)
            position["net_if_closed_now"] = round(net, 2)
            position["checked_at"] = now_ist.strftime("%d %b %H:%M:%S IST")
            if net >= TAKE_PROFIT:
                pnl = await close_all("take_profit", prices)
                ctx.note("exited", signal="COVER", reason=(
                    f"take-profit: Rs {net:,.0f} after charges at bid/ask (target Rs {TAKE_PROFIT:,.0f}); "
                    f"P&L {pnl:+,.0f} before charges; next entry from {now_ist + PAUSE_AFTER_CLOSE:%H:%M:%S}"
                ))
                ctx.wake_at(now_utc + PAUSE_AFTER_CLOSE)
                return
        if weekly < today or (weekly == today and now_ist.time() >= WEEKLY_CLOSE):
            if prices is None:
                prices = await forced_prices()
            pnl = await close_all("weekly_expiry_3pm", prices)
            ctx.note("exited", signal="COVER", reason=(
                f"the {weekly:%d %b} weekly expiring: all four closed at bid/ask, P&L {pnl:+,.0f} before charges; "
                f"next entry from {ENTRY_TIME:%H:%M} on the next trading day"
            ))
            return
        if missing:
            ctx.note("hold", reason=f"{REGIME} open; no bid/ask for {', '.join(missing)} -- take-profit checked once all four have one")
        else:
            ctx.note("hold", reason=(
                f"{REGIME} {position['strike']:,.0f} ({BIAS}; weekly {weekly:%d %b}, hedge "
                f"{hedge_of(position):%d %b}): Rs {net:,.0f} if closed now at bid/ask after charges; "
                f"take-profit at Rs {TAKE_PROFIT:,.0f}; all four close {weekly:%d %b} {WEEKLY_CLOSE:%H:%M}"
            ))
        ctx.wake_at(now_utc + RECHECK)
        return

    if not ENTRY_TIME <= now_ist.time() < ENTRY_UNTIL:
        ctx.note("skipped", reason=f"outside entry window ({ENTRY_TIME:%H:%M}-{ENTRY_UNTIL:%H:%M} IST)")
        return
    paused_until = ctx.state.get("paused_until")
    if paused_until and now_utc < (resume := datetime.fromisoformat(paused_until)):
        ctx.note("skipped", reason=f"paused a minute after the last close: next entry from {resume.astimezone(IST):%H:%M:%S}")
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
    weekly = expiries[1] if today == expiries[0] and len(expiries) > 1 else expiries[0]
    if weekly == today:
        ctx.note("skipped", reason="only today's expiry is listed -- nothing for a later week")
        return
    hedge = hedge_after(expiries, weekly)
    if hedge is None:
        ctx.note("skipped", reason=f"no NIFTY expiry listed two weeks after {weekly:%d %b} yet for the hedge")
        return
    strike = atm_strike(spot)

    chosen: dict[str, tuple[Instrument, float]] = {}
    for name, option_type, order_side, which in LEGS:
        expiry = weekly if which == "weekly" else hedge
        instrument = await ctx.find_option(underlying.id, expiry, strike, option_type)
        if instrument is None:
            ctx.note("skipped", reason=f"no {expiry:%d %b} {strike:,.0f} {option_type} in the catalog yet")
            return
        quote = await bid_ask(ctx, instrument.id)
        if quote is None:
            ctx.note("skipped", reason=f"no bid/ask for the {expiry:%d %b} {strike:,.0f} {option_type} yet")
            return
        chosen[name] = (instrument, quote[0] if order_side == "sell" else quote[1])

    legs, entry_charges = {}, 0.0
    for name, option_type, order_side, which in LEGS:
        instrument, price = chosen[name]
        quantity = float(LOTS_PER_LEG * (instrument.lot_size or DEFAULT_LOT_SIZE))
        await ctx.open_leg(instrument, order_side, quantity, price)
        legs[name] = {"instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": order_side,
                      "expiry": (weekly if which == "weekly" else hedge).isoformat(), "quantity": quantity, "entry_price": price}
        entry_charges += charges_of(legs[name], price, buying=order_side == "buy")

    ctx.state["position"] = {
        "regime": REGIME,
        "bias": BIAS,
        "strike": strike,
        "entry_spot": spot,
        "hedge_rule": HEDGE_RULE,
        "weekly_expiry": weekly.isoformat(),
        "hedge_expiry": hedge.isoformat(),
        "take_profit": TAKE_PROFIT,
        "entry_charges": round(entry_charges, 2),
        "legs": legs,
        "opened_at": now_utc.isoformat(),
    }
    ctx.state.pop("paused_until", None)
    debit = sum(leg["entry_price"] * leg["quantity"] * (1 if leg["side"] == "buy" else -1) for leg in legs.values())
    ctx.note("entered", signal="SYNTHETIC", reason=(
        rule_note + f"{REGIME} opened at {strike:,.0f}, NIFTY {spot:,.1f}: "
        + "; ".join(f"{'buy' if leg['side'] == 'buy' else 'sell'} {date.fromisoformat(leg['expiry']):%d %b} {leg['option_type']} "
                    f"{leg['entry_price']:,.2f} ({'ask' if leg['side'] == 'buy' else 'bid'})" for leg in legs.values())
        + f"; net {'debit' if debit >= 0 else 'credit'} Rs {abs(debit):,.0f}; {LOTS_PER_LEG} lots a leg; "
        f"take-profit Rs {TAKE_PROFIT:,.0f}; all four close {weekly:%d %b} {WEEKLY_CLOSE:%H:%M}"
    ))
    ctx.wake_at(now_utc + RECHECK)
