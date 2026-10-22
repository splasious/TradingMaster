"""
Conti Straddle 15 MIN -- a NIFTY short straddle held all session (native)
=========================================================================

AM OP TRD 15 MIN's straddle without its PCR band or its spreads (agreed
21 Oct): from 09:45 to 15:00 IST a short straddle -- sell a CE and a PE at
the same strike, 10 lots each -- is always open, whatever the PCR. The PCR
only picks the strike, from the 50-point strikes around NIFTY by their live
premiums:

  PCR below 1     the strike whose CALL premium is just above its PUT's
                  (the CE minus the PE is positive and the smallest) --
                  normally the strike just below NIFTY.
  PCR 1 or above  the strike whose PUT premium is just above its CALL's --
                  normally the strike just above NIFTY.

Nearer NIFTY on a tie. The strikes looked at are the one nearest NIFTY and
CANDIDATE_STEPS either side; one whose two premiums aren't both live is
passed over. None fitting: no straddle that check (a note says why), and the
next check tries again.

Rolls -- closes and reopens -- on a finished candle, not the live price
(22 Oct, as AM OP TRD 15 MIN and Conti Ladder Straddle 15 MIN): when a
15-minute NIFTY candle that closed after the straddle opened (or last
rolled) closes ROLL_TRIGGER (100) points or more from where NIFTY was
then; however far NIFTY goes between candle closes, nothing moves. The
roll reads the PCR afresh for its strike (the previous side if there's no
PCR record yet). The side chosen at entry is otherwise kept: the
PCR crossing 1 doesn't move the straddle. Entries from 09:45; everything
closes at 15:00, no exceptions; nothing held overnight. Current week's
weekly expiry, next week's on the expiry day itself. 10 lots per leg
(Instrument.lot_size; DEFAULT_LOT_SIZE only as a fallback). No PCR exit,
no MA exit, no spreads.

PCR is ctx.get_pcr(timeframe="15m"), as AM OP TRD 15 MIN reads it; candles
ctx.get_candles(..., "15m"), finished ones only.
Runs through services/paper_trading/native_runner.py.
"""

import uuid
from datetime import datetime, time as dtime, timedelta

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST

UNDERLYING_SYMBOL = "NIFTY 50"
PCR_TIMEFRAME = "15m"
CANDLE_TIMEFRAME = "15m"
CANDLE = timedelta(minutes=15)
STRIKE_STEP = 50
CANDIDATE_STEPS = 3  # strikes looked at either side of the one nearest NIFTY
LOTS_PER_LEG = 10
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 45)
EXIT_TIME = dtime(15, 0)
ROLL_TRIGGER = 100  # points a finished 15-min close is from the straddle's entry NIFTY before it rolls
PCR_PIVOT = 1.0


def side_for(pcr: float) -> str:
    """"call": the CE premium just above the PE; "put": the PE just above the CE."""
    return "call" if pcr < PCR_PIVOT else "put"


def candidate_strikes(spot: float) -> list[float]:
    nearest = round(spot / STRIKE_STEP) * STRIKE_STEP
    return [nearest + STRIKE_STEP * k for k in range(-CANDIDATE_STEPS, CANDIDATE_STEPS + 1)]


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

    async def close_position(exit_reason: str) -> None:
        trade_legs = []
        total_pnl = 0.0
        total_entry_notional = 0.0
        for leg in position["legs"].values():
            instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
            exit_price = await ctx.get_price(instrument.id) if instrument else None
            if exit_price is None:
                exit_price = leg["entry_price"]
            await ctx.close_leg(instrument, "buy", leg["quantity"], exit_price)  # both legs are short
            total_pnl += (leg["entry_price"] - exit_price) * leg["quantity"]
            total_entry_notional += leg["entry_price"] * leg["quantity"]
            trade_legs.append({
                "instrument_id": leg["instrument_id"], "side": "short", "quantity": leg["quantity"],
                "entry_price": leg["entry_price"], "exit_price": exit_price,
            })
        pnl_pct = (total_pnl / total_entry_notional * 100) if total_entry_notional else 0.0
        await ctx.record_trade(
            legs=trade_legs, pnl=total_pnl, pnl_pct=pnl_pct, exit_reason=exit_reason,
            opened_at=datetime.fromisoformat(position["opened_at"]),
        )
        ctx.state["position"] = None

    is_rollover = False
    previous_side = None
    if position is not None:
        if force_exit:
            await close_position("manual")
            ctx.note("exited", signal="COVER", reason="manual exit")
            return
        if past_exit_cutoff(now_ist.time()):
            await close_position("time_cutoff_3pm")
            ctx.note("exited", signal="COVER", reason="3:00pm IST cutoff")
            return
        entry_spot = position["entry_spot"]
        opened_at = datetime.fromisoformat(position["opened_at"])
        candles = await ctx.get_candles(underlying.id, CANDLE_TIMEFRAME, limit=1)
        last = candles[-1] if candles else None
        # Only a candle that finished after the straddle opened can roll it.
        closed = last if last is not None and last["ts"] + CANDLE > opened_at else None
        moved = abs(closed["close"] - entry_spot) if closed is not None else 0.0
        if closed is None or moved < ROLL_TRIGGER:
            close_text = (
                f"{closed['ts'].astimezone(IST) + CANDLE:%H:%M} close {closed['close']:,.1f} ({moved:.1f}pt from entry)"
                if closed is not None else "no 15-min candle closed since it opened"
            )
            ctx.note("hold", reason=(
                f"{position['side']}-heavy straddle {position['strike']:,.0f} open, NIFTY {spot:,.1f}, entry "
                f"{entry_spot:,.1f}; {close_text}; rolls at {ROLL_TRIGGER}"
            ))
            return
        previous_side = position["side"]
        await close_position("rollover")
        is_rollover = True
        position = None  # reopened below, this same check

    if not in_entry_window(now_ist.time()):
        ctx.note("skipped", reason=f"outside entry window ({ENTRY_TIME}-{EXIT_TIME} IST)")
        return

    pcr = await ctx.get_pcr(timeframe=PCR_TIMEFRAME)
    if pcr is None and not is_rollover:
        ctx.note("skipped", reason="no PCR data available yet")
        return
    side = side_for(pcr) if pcr is not None else previous_side

    today = now_ist.date()
    expiries = await ctx.list_weekly_expiries(underlying.id, today)
    if not expiries:
        ctx.note("skipped", reason="no live NIFTY option expiries found")
        return
    expiry = expiries[0]
    if today == expiry and len(expiries) > 1:
        expiry = expiries[1]  # next week's on the expiry date itself

    contracts: dict[float, dict[str, tuple[Instrument, float]]] = {}
    for strike in candidate_strikes(spot):
        pair = {}
        for option_type in ("CE", "PE"):
            instrument = await ctx.find_option(underlying.id, expiry, strike, option_type)
            price = await ctx.get_price(instrument.id) if instrument is not None else None
            if price is None:
                break
            pair[option_type] = (instrument, price)
        if len(pair) == 2:
            contracts[strike] = pair
    quotes = {strike: (pair["CE"][1], pair["PE"][1]) for strike, pair in contracts.items()}
    strike = pick_strike(quotes, side, spot)
    if strike is None:
        heavier = "CALL" if side == "call" else "PUT"
        ctx.note("skipped", reason=(
            f"PCR {f'{pcr:.3f}' if pcr is not None else '—'}: no strike within {STRIKE_STEP * CANDIDATE_STEPS} of NIFTY with the "
            f"{heavier} premium above the other ({len(quotes)} with both premiums live)"
        ))
        return

    ce_instrument, ce_price = contracts[strike]["CE"]
    pe_instrument, pe_price = contracts[strike]["PE"]
    quantity = float(LOTS_PER_LEG * (ce_instrument.lot_size or DEFAULT_LOT_SIZE))
    await ctx.open_leg(ce_instrument, "sell", quantity, ce_price)
    await ctx.open_leg(pe_instrument, "sell", quantity, pe_price)

    ctx.state["position"] = {
        "regime": "sideways",  # a straddle, as the card and the trade records call it
        "side": side,
        "strike": strike,
        "pcr_at_entry": pcr,
        "entry_spot": spot,
        "expiry": expiry.isoformat(),
        "roll_trigger": ROLL_TRIGGER,
        "exit_time": EXIT_TIME.strftime("%H:%M"),
        "legs": {
            "short_ce": {"instrument_id": str(ce_instrument.id), "strike": strike, "option_type": "CE", "side": "sell",
                         "quantity": quantity, "entry_price": ce_price},
            "short_pe": {"instrument_id": str(pe_instrument.id), "strike": strike, "option_type": "PE", "side": "sell",
                         "quantity": quantity, "entry_price": pe_price},
        },
        "opened_at": now_utc.isoformat(),
    }
    pcr_text = f"PCR {pcr:.3f}" if pcr is not None else "no PCR yet, the previous side"
    ctx.note("entered", signal="STRADDLE", reason=(
        f"{side}-heavy straddle {'rolled to' if is_rollover else 'opened at'} {strike:,.0f} ({pcr_text}): "
        f"sell CE {ce_price:,.2f} + PE {pe_price:,.2f}"
    ))
