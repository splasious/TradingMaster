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

Steps with NIFTY, trading only the legs that change (22 Oct, version 2):
the ladder keeps a reference level -- NIFTY at the opening. When live
NIFTY is STEP (100) points or more from it, the ladder steps 100 points
that way: the straddle at the far end closes and a new one, 2 lots a leg,
opens 100 points beyond the near end; the other four stay as they are.
The reference and the middle strike move exactly 100 with it, so the next
step comes 100 further on, and a jump of 230 points steps twice in one
check. A step needs live CE and PE prices at the new strike; until it has
them nothing moves, the far straddle included. The PCR is read only for
the opening. Each straddle is a trade of its own, recorded when it closes
-- at a step, or at 15:00 with the rest.

Entries from 09:45; everything closes at 15:00, no exceptions; nothing
held overnight. Current week's weekly expiry, next week's on the expiry
day itself. Lots from Instrument.lot_size (DEFAULT_LOT_SIZE only as a
fallback). No PCR exit, no MA exit, no spreads.

PCR is ctx.get_pcr(timeframe="15m"). Runs through
services/paper_trading/native_runner.py.
"""

import uuid
from datetime import date, datetime, time as dtime

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST

UNDERLYING_SYMBOL = "NIFTY 50"
PCR_TIMEFRAME = "15m"
STRIKE_STEP = 50
CANDIDATE_STEPS = 3  # strikes looked at either side of the one nearest NIFTY, for the middle strike
LADDER = (-200, -100, 0, 100, 200)  # each straddle's strike, from the middle one
LOTS_PER_LEG = 2  # per straddle: 5 straddles, so 10 lots a side in all
DEFAULT_LOT_SIZE = 65  # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 45)
EXIT_TIME = dtime(15, 0)
STEP = 100  # live NIFTY this far from the reference steps the ladder this far; also the ladder's spacing
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

    async def close_straddle(strike: float, exit_reason: str) -> None:
        """One straddle's two legs bought back and recorded as a trade."""
        legs = [leg for leg in position["legs"].values() if leg["strike"] == strike]
        trade_legs = []
        total_pnl = 0.0
        total_entry_notional = 0.0
        for leg in legs:
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
            opened_at=datetime.fromisoformat(legs[0].get("opened_at") or position["opened_at"]),
        )
        for option_type in ("CE", "PE"):
            position["legs"].pop(leg_key(strike, option_type), None)
        position["strikes"] = [s for s in position["strikes"] if s != strike]

    async def close_position(exit_reason: str) -> None:
        for strike in list(position["strikes"]):
            await close_straddle(strike, exit_reason)
        ctx.state["position"] = None

    if position is not None:
        if force_exit:
            await close_position("manual")
            ctx.note("exited", signal="COVER", reason="manual exit")
            return
        if past_exit_cutoff(now_ist.time()):
            await close_position("time_cutoff_3pm")
            ctx.note("exited", signal="COVER", reason="3:00pm IST cutoff")
            return
        reference = position.get("reference", position["entry_spot"])
        steps = int((spot - reference) / STEP)  # whole steps, toward zero
        taken = []
        waiting = None
        for _ in range(abs(steps)):
            up = steps > 0
            near, far = (max(position["strikes"]), min(position["strikes"])) if up else (min(position["strikes"]), max(position["strikes"]))
            new = near + STEP if up else near - STEP
            pair = {}
            for option_type in ("CE", "PE"):
                instrument = await ctx.find_option(underlying.id, date.fromisoformat(position["expiry"]), new, option_type)
                price = await ctx.get_price(instrument.id) if instrument is not None else None
                if price is None:
                    break
                pair[option_type] = (instrument, price)
            if len(pair) < 2:
                waiting = new
                break
            await close_straddle(far, "rollover")
            for option_type, (instrument, price) in pair.items():
                quantity = float(LOTS_PER_LEG * (instrument.lot_size or DEFAULT_LOT_SIZE))
                await ctx.open_leg(instrument, "sell", quantity, price)
                position["legs"][leg_key(new, option_type)] = {
                    "instrument_id": str(instrument.id), "strike": new, "option_type": option_type, "side": "sell",
                    "quantity": quantity, "entry_price": price, "opened_at": now_utc.isoformat(),
                }
            position["strikes"] = sorted(position["strikes"] + [new])
            position["strike"] += STEP if up else -STEP
            reference += STEP if up else -STEP
            position["reference"] = reference
            taken.append((far, new))
        ctx.state["position"] = position
        ladder = f"{position['strikes'][0]:,.0f}-{position['strikes'][-1]:,.0f} (middle {position['strike']:,.0f})"
        if taken:
            moves = ", ".join(f"{far:,.0f} closed / {new:,.0f} opened" for far, new in taken)
            ctx.note("entered", signal="STRADDLE", reason=(
                f"ladder stepped {len(taken)}x{STEP} with NIFTY {spot:,.1f}: {moves}; now {ladder}, next step at "
                f"{reference - STEP:,.1f} / {reference + STEP:,.1f}"
                + (f"; no live CE and PE price at {waiting:,.0f} for the next step yet" if waiting is not None else "")
            ))
            return
        ctx.note("hold", reason=(
            f"{position['side']}-heavy ladder {ladder} open, NIFTY {spot:,.1f} vs reference {reference:,.1f} "
            f"({spot - reference:+.1f}pt); steps at {reference - STEP:,.1f} / {reference + STEP:,.1f}"
            + (f"; no live CE and PE price at {waiting:,.0f} to step to yet" if waiting is not None else "")
        ))
        return

    if not in_entry_window(now_ist.time()):
        ctx.note("skipped", reason=f"outside entry window ({ENTRY_TIME}-{EXIT_TIME} IST)")
        return

    pcr = await ctx.get_pcr(timeframe=PCR_TIMEFRAME)
    if pcr is None:
        ctx.note("skipped", reason="no PCR data available yet")
        return
    side = side_for(pcr)

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
                "quantity": quantity, "entry_price": price, "opened_at": now_utc.isoformat(),
            }

    ctx.state["position"] = {
        "regime": "sideways",  # straddles, as the card and the trade records call them
        "side": side,
        "strike": middle,
        "strikes": strikes,
        "pcr_at_entry": pcr,
        "entry_spot": spot,
        "reference": spot,
        "expiry": expiry.isoformat(),
        "roll_trigger": STEP,
        "exit_time": EXIT_TIME.strftime("%H:%M"),
        "legs": legs,
        "opened_at": now_utc.isoformat(),
    }
    credit = sum(leg["entry_price"] for leg in legs.values())
    ctx.note("entered", signal="STRADDLE", reason=(
        f"{side}-heavy ladder opened at {strikes[0]:,.0f}-{strikes[-1]:,.0f}, middle {middle:,.0f} (PCR {pcr:.3f}): "
        f"5 straddles of {LOTS_PER_LEG} lots, premiums {credit:,.2f} in all"
    ))
