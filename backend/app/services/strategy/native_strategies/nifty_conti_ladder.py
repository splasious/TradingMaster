"""
Conti Ladder Straddle 15 MIN -- five NIFTY short straddles held all session (native)
==================================================================================

Conti Straddle 15 MIN's single straddle spread over a ladder (agreed 22
Oct): from 09:45 to 15:00 IST five short straddles are always open --
sell a CE and a PE, 2 lots each, at the middle strike and at 100 and 200
points either side of it, so 10 lots a side in all, as Conti's one
straddle has. The PCR picks the middle strike as Conti picks its strike,
from the 50-point strikes around NIFTY by their live premiums:

  PCR below 1     the strike whose CALL premium is just above its PUT's
                  (the CE minus the PE is positive and the smallest)
  PCR 1 or above  the strike whose PUT premium is just above its CALL's

Nearer NIFTY on a tie; the strikes looked at are the one nearest NIFTY and
CANDIDATE_STEPS either side. The ladder opens only when all ten of its
contracts have a live price; otherwise no ladder that check (a note says
why), and the next check tries again.

Shifts on a finished candle, not on the live price: when a 15-minute
NIFTY candle that closed after the ladder opened closes ROLL_TRIGGER (100)
points or more from where NIFTY was at the opening, all ten legs close and
the ladder reopens around a fresh middle strike, the PCR read again (the
previous side if there's no PCR record yet). However far NIFTY goes
between candle closes, nothing moves. Entries from 09:45; everything
closes at 15:00, no exceptions; nothing held overnight. Current week's
weekly expiry, next week's on the expiry day itself. Lots from
Instrument.lot_size (DEFAULT_LOT_SIZE only as a fallback). No PCR exit,
no MA exit, no spreads.

PCR is ctx.get_pcr(timeframe="15m"); candles ctx.get_candles(..., "15m"),
finished ones only. Runs through services/paper_trading/native_runner.py.
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
CANDIDATE_STEPS = 3  # strikes looked at either side of the one nearest NIFTY, for the middle strike
LADDER = (-200, -100, 0, 100, 200)  # each straddle's strike, from the middle one
LOTS_PER_LEG = 2  # per straddle: 5 straddles, so 10 lots a side in all
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 45)
EXIT_TIME = dtime(15, 0)
ROLL_TRIGGER = 100  # points a finished 15-min close is from the ladder's opening NIFTY before it shifts
PCR_PIVOT = 1.0


def side_for(pcr: float) -> str:
    """"call": the CE premium just above the PE; "put": the PE just above the CE."""
    return "call" if pcr < PCR_PIVOT else "put"


def candidate_strikes(spot: float) -> list[float]:
    nearest = round(spot / STRIKE_STEP) * STRIKE_STEP
    return [nearest + STRIKE_STEP * k for k in range(-CANDIDATE_STEPS, CANDIDATE_STEPS + 1)]


def ladder_strikes(middle: float) -> list[float]:
    return [middle + offset for offset in LADDER]


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


def leg_key(strike: float, option_type: str) -> str:
    return f"short_{strike:.0f}_{option_type.lower()}"


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
            await ctx.close_leg(instrument, "buy", leg["quantity"], exit_price)  # every leg is short
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
        # Only a candle that finished after the ladder opened can shift it.
        closed = last if last is not None and last["ts"] + CANDLE > opened_at else None
        moved = abs(closed["close"] - entry_spot) if closed is not None else 0.0
        if closed is None or moved < ROLL_TRIGGER:
            close_text = (
                f"{closed['ts'].astimezone(IST) + CANDLE:%H:%M} close {closed['close']:,.1f} ({moved:.1f}pt from entry)"
                if closed is not None else "no 15-min candle closed since it opened"
            )
            ctx.note("hold", reason=(
                f"{position['side']}-heavy ladder {position['strikes'][0]:,.0f}-{position['strikes'][-1]:,.0f} "
                f"(middle {position['strike']:,.0f}) open, entry NIFTY {entry_spot:,.1f}; {close_text}; shifts at {ROLL_TRIGGER}"
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

    async def priced(strike: float) -> dict[str, tuple[Instrument, float]] | None:
        if strike not in contracts:
            pair = {}
            for option_type in ("CE", "PE"):
                instrument = await ctx.find_option(underlying.id, expiry, strike, option_type)
                price = await ctx.get_price(instrument.id) if instrument is not None else None
                if price is None:
                    break
                pair[option_type] = (instrument, price)
            contracts[strike] = pair
        return contracts[strike] if len(contracts[strike]) == 2 else None

    quotes = {}
    for strike in candidate_strikes(spot):
        pair = await priced(strike)
        if pair is not None:
            quotes[strike] = (pair["CE"][1], pair["PE"][1])
    middle = pick_strike(quotes, side, spot)
    if middle is None:
        heavier = "CALL" if side == "call" else "PUT"
        ctx.note("skipped", reason=(
            f"PCR {f'{pcr:.3f}' if pcr is not None else '—'}: no strike within {STRIKE_STEP * CANDIDATE_STEPS} of NIFTY with the "
            f"{heavier} premium above the other ({len(quotes)} with both premiums live)"
        ))
        return

    strikes = ladder_strikes(middle)
    missing = [s for s in strikes if await priced(s) is None]
    if missing:
        ctx.note("skipped", reason=(
            f"ladder around {middle:,.0f}: no live CE and PE price at {', '.join(f'{s:,.0f}' for s in missing)} -- "
            "it opens only with all ten legs priced"
        ))
        return

    legs = {}
    for strike in strikes:
        for option_type in ("CE", "PE"):
            instrument, price = contracts[strike][option_type]
            quantity = float(LOTS_PER_LEG * (instrument.lot_size or DEFAULT_LOT_SIZE))
            await ctx.open_leg(instrument, "sell", quantity, price)
            legs[leg_key(strike, option_type)] = {
                "instrument_id": str(instrument.id), "strike": strike, "option_type": option_type, "side": "sell",
                "quantity": quantity, "entry_price": price,
            }

    ctx.state["position"] = {
        "regime": "sideways",  # straddles, as the card and the trade records call them
        "side": side,
        "strike": middle,
        "strikes": strikes,
        "pcr_at_entry": pcr,
        "entry_spot": spot,
        "expiry": expiry.isoformat(),
        "roll_trigger": ROLL_TRIGGER,
        "exit_time": EXIT_TIME.strftime("%H:%M"),
        "legs": legs,
        "opened_at": now_utc.isoformat(),
    }
    pcr_text = f"PCR {pcr:.3f}" if pcr is not None else "no PCR yet, the previous side"
    credit = sum(leg["entry_price"] for leg in legs.values())
    ctx.note("entered", signal="STRADDLE", reason=(
        f"{side}-heavy ladder {'shifted to' if is_rollover else 'opened at'} {strikes[0]:,.0f}-{strikes[-1]:,.0f}, "
        f"middle {middle:,.0f} ({pcr_text}): 5 straddles of {LOTS_PER_LEG} lots, premiums {credit:,.2f} in all"
    ))
