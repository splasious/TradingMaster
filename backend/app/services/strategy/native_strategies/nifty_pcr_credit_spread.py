"""
Nifty PCR Credit Spread Strategy (native, unsandboxed)
=======================================================

  Bias:      PCR (Put OI / Call OI, summed across the nearest 4 weekly
             expiries) < 1 -> bearish, > 1 -> bullish
  Bearish:   Bear Call Spread  -> sell ATM CE, buy CE 200 points higher
  Bullish:   Bull Put Spread   -> sell ATM PE, buy PE 200 points lower
  Strikes:   always rounded to end in "00" (skip the intermediate
             50-point strikes)
  Entry:     from 9:45 AM IST onward
  Exit:      close everything by 3:00 PM IST regardless, OR earlier if
             the PCR bias flips, OR the short strike is breached
  Rollover:  if spot drifts 100+ points from the current short strike
             (i.e. the ATM strike itself has moved), close the current
             spread and immediately reopen at the new ATM, same bias --
             repeats as many times as needed through the day, until the
             PCR bias actually flips (a real exit, not another roll)
  Expiry:    current week's weekly expiry by default; on the expiry
             date itself, roll into next week's expiry instead
  Size:      2 lots on each leg, fixed (not a % of pool capital)

Runs through services/paper_trading/native_runner.py -- `ctx` gives this
module everything it needs (live prices, PCR, option-contract lookup,
leg open/close bookkeeping) without a direct AsyncSession or import of
this platform's ORM session machinery. See that module's docstring for
why this runs trusted/unsandboxed rather than through the RestrictedPython
sandbox: it needs real DB access and a 2-leg position, neither of which
the sandbox's generate_signal(candles, params) contract can express.
"""

import uuid
from datetime import datetime, time as dtime

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST

STRIKE_STEP = 100
SPREAD_WIDTH = 200
LOTS_PER_LEG = 2
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 45)
EXIT_TIME = dtime(15, 0)
UNDERLYING_SYMBOL = "NIFTY 50"


def round_to_nearest_100(spot: float, tie_break: str = "up") -> float:
    lower = (spot // STRIKE_STEP) * STRIKE_STEP
    upper = lower + STRIKE_STEP
    dist_lower = spot - lower
    dist_upper = upper - spot
    if dist_lower < dist_upper:
        return lower
    if dist_upper < dist_lower:
        return upper
    return upper if tie_break == "up" else lower


def determine_bias(pcr: float) -> str:
    if pcr < 1:
        return "bearish"
    if pcr > 1:
        return "bullish"
    return "neutral"


def in_entry_window(t: dtime) -> bool:
    return ENTRY_TIME <= t < EXIT_TIME


def past_exit_cutoff(t: dtime) -> bool:
    return t >= EXIT_TIME


async def evaluate(ctx) -> None:
    now_utc = ctx.now
    now_ist = now_utc.astimezone(IST)

    underlying = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == UNDERLYING_SYMBOL))
    ).scalar_one_or_none()
    if underlying is None:
        ctx.note("skipped", reason=f"{UNDERLYING_SYMBOL} instrument not found")
        return

    force_exit = ctx.state.pop("force_exit", False)
    position = ctx.state.get("position")

    spot = await ctx.get_price(underlying.id)
    if spot is None:
        ctx.note("skipped", reason="no live NIFTY spot price available")
        return

    async def close_position(exit_reason: str, short_instrument, long_instrument, short_price, long_price) -> None:
        short_leg = position["short"]
        long_leg = position["long"]
        sp = short_price if short_price is not None else short_leg["entry_price"]
        lp = long_price if long_price is not None else long_leg["entry_price"]
        await ctx.close_leg(short_instrument, "buy", short_leg["quantity"], sp)
        await ctx.close_leg(long_instrument, "sell", long_leg["quantity"], lp)

        entry_credit = short_leg["entry_price"] - long_leg["entry_price"]
        exit_debit = sp - lp
        pnl = (entry_credit - exit_debit) * short_leg["quantity"]
        denom = short_leg["entry_price"] * short_leg["quantity"]
        pnl_pct = (pnl / denom * 100) if denom else 0.0

        await ctx.record_trade(
            legs=[
                {
                    "instrument_id": short_leg["instrument_id"], "side": "short", "quantity": short_leg["quantity"],
                    "entry_price": short_leg["entry_price"], "exit_price": sp,
                },
                {
                    "instrument_id": long_leg["instrument_id"], "side": "long", "quantity": long_leg["quantity"],
                    "entry_price": long_leg["entry_price"], "exit_price": lp,
                },
            ],
            pnl=pnl, pnl_pct=pnl_pct, exit_reason=exit_reason,
            opened_at=datetime.fromisoformat(position["opened_at"]),
        )
        ctx.state["position"] = None

    if position is not None:
        short_instrument = await ctx.db.get(Instrument, uuid.UUID(position["short"]["instrument_id"]))
        long_instrument = await ctx.db.get(Instrument, uuid.UUID(position["long"]["instrument_id"]))
        short_price = await ctx.get_price(short_instrument.id)
        long_price = await ctx.get_price(long_instrument.id)
        bias = position["bias"]

        if force_exit:
            await close_position("manual", short_instrument, long_instrument, short_price, long_price)
            ctx.note("exited", signal="COVER", reason="manual exit")
            return

        if past_exit_cutoff(now_ist.time()):
            await close_position("time_cutoff_3pm", short_instrument, long_instrument, short_price, long_price)
            ctx.note("exited", signal="COVER", reason="3:00pm IST cutoff")
            return

        pcr = await ctx.get_pcr()
        current_bias = determine_bias(pcr) if pcr is not None else "neutral"
        if current_bias != "neutral" and current_bias != bias:
            await close_position(f"pcr_flipped_to_{current_bias}", short_instrument, long_instrument, short_price, long_price)
            ctx.note("exited", signal="COVER", reason=f"PCR bias flipped to {current_bias}")
            return

        if spot is not None:
            new_atm = round_to_nearest_100(spot)
            if new_atm != position["short"]["strike"]:
                await close_position("rollover", short_instrument, long_instrument, short_price, long_price)
                position = None  # fall through to open a fresh spread at the new ATM below, same tick
            else:
                short_strike = position["short"]["strike"]
                breached = (
                    (bias == "bearish" and spot >= short_strike) or (bias == "bullish" and spot <= short_strike)
                )
                if breached:
                    await close_position("short_strike_tested", short_instrument, long_instrument, short_price, long_price)
                    ctx.note("exited", signal="COVER", reason="short strike breached")
                    return
                ctx.note("hold", reason=f"{bias} spread open, short strike {short_strike}")
                return
        else:
            ctx.note("hold", reason="spread open, no live spot to check exit conditions this tick")
            return

    if not in_entry_window(now_ist.time()):
        ctx.note("skipped", reason=f"outside entry window ({ENTRY_TIME}-{EXIT_TIME} IST)")
        return

    pcr = await ctx.get_pcr()
    if pcr is None:
        ctx.note("skipped", reason="no PCR data available yet")
        return
    bias = determine_bias(pcr)
    if bias == "neutral":
        ctx.note("skipped", reason="PCR exactly 1 -- no directional edge")
        return

    today = now_ist.date()
    expiries = await ctx.list_weekly_expiries(underlying.id, today)
    if not expiries:
        ctx.note("skipped", reason="no live NIFTY option expiries found")
        return
    expiry = expiries[0]
    if today == expiry and len(expiries) > 1:
        expiry = expiries[1]  # roll to next week on the expiry date itself

    atm = round_to_nearest_100(spot)
    if bias == "bearish":
        short_strike, long_strike, option_type = atm, atm + SPREAD_WIDTH, "CE"
    else:
        short_strike, long_strike, option_type = atm, atm - SPREAD_WIDTH, "PE"

    short_instrument = await ctx.find_option(underlying.id, expiry, short_strike, option_type)
    long_instrument = await ctx.find_option(underlying.id, expiry, long_strike, option_type)
    if short_instrument is None or long_instrument is None:
        ctx.note("skipped", reason=f"option contracts not found for {expiry} {short_strike}/{long_strike} {option_type}")
        return

    short_price = await ctx.get_price(short_instrument.id)
    long_price = await ctx.get_price(long_instrument.id)
    if short_price is None or long_price is None:
        ctx.note("skipped", reason="no live premium yet for the resolved option legs")
        return

    lot_size = short_instrument.lot_size or DEFAULT_LOT_SIZE
    quantity = float(LOTS_PER_LEG * lot_size)

    await ctx.open_leg(short_instrument, "sell", quantity, short_price)
    await ctx.open_leg(long_instrument, "buy", quantity, long_price)

    ctx.state["position"] = {
        "bias": bias,
        "pcr_at_entry": pcr,
        "expiry": expiry.isoformat(),
        "short": {
            "instrument_id": str(short_instrument.id), "strike": short_strike, "quantity": quantity,
            "entry_price": short_price,
        },
        "long": {
            "instrument_id": str(long_instrument.id), "strike": long_strike, "quantity": quantity,
            "entry_price": long_price,
        },
        "opened_at": now_utc.isoformat(),
    }
    ctx.note(
        "entered", signal=("SHORT_CE" if bias == "bearish" else "SHORT_PE"),
        reason=f"{bias} spread: sell {short_strike}{option_type} / buy {long_strike}{option_type}",
    )
