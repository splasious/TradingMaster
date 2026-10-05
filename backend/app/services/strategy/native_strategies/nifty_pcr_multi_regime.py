"""
AM OP TRD 15 MIN -- Nifty PCR Multi-Regime Options Strategy (native, unsandboxed)
==================================================================================

A PCR-driven state machine with three mutually exclusive regimes, decided
purely from the current (15m-timeframe) PCR reading:

  SIDEWAYS  (0.80 <= PCR <= 1.20)
      Naked short straddle: sell ATM CE + sell ATM PE, no hedge wings --
      unlike the iron condor this originally was, there is no defined cap
      on loss if spot makes a large move against either leg before the
      100-point rollover check or the 3:00pm hard exit catches it (see
      ROLL_TRIGGER and EXIT_TIME below; removed on request). Exit if PCR
      later prints > 1.25 or < 0.75.

  BULLISH   (PCR > 1.25)
      Bull put credit spread: sell PE at ATM-200, buy PE at ATM-400.
      Exit when PCR falls back below 1.20.

  BEARISH   (PCR < 0.75)
      Bear call credit spread: sell CE at ATM+200, buy CE at ATM+400.
      Exit when PCR rises back above 0.80.

0.75<->0.80 and 1.20<->1.25 are hysteresis buffers: PCR sitting right on a
boundary can't flip the position every tick, and PCR sitting IN a buffer
zone with no open position just stays flat (no edge either way).

Rolling: closes and reopens the SAME regime at the new ATM once spot has
moved ROLL_TRIGGER (100) points from the spot recorded when the position
was last opened/rolled -- a uniform 100 points for all three position
types (the original spec split this 100 for the iron condor / 200 for the
directional spreads; unified to 100 across the board on request). This is
gated on raw spot distance from that recorded entry_spot, NOT on comparing
already-rounded ATM strikes -- the version originally pasted into this
slot compared rounded strikes, which quantizes the effective trigger to
roughly half the configured distance (verified: fired at ~51pt instead of
100, ~150pt instead of 200). Fixed here by tracking entry_spot (a float)
and comparing the real distance before any rounding happens.

Strike rounding also had a second bug in the original: it used Python's
round(), which resolves an exact .5 tie to the *nearest even* strike
(banker's rounding) -- e.g. 24850 rounds down to 24800 but 24950 rounds up
to 25000, with no way to predict which without knowing strike parity.
Replaced with an explicit tie_break rule (same approach as
nifty_pcr_credit_spread.py's round_to_nearest_100), so ties always resolve
the same direction.

PCR is read via ctx.get_pcr(timeframe="15m") -- explicit 15-minute
evaluation, matching this strategy's name (ctx.get_pcr's own default is
already "15m", so this is also just making that intent visible in the
code itself).

A roll's reopen always reuses the regime the closed position already was,
never re-derives it fresh from the current PCR reading. Necessary here
(unlike a simpler 2-way bearish/bullish strategy) because this design has
a third "buffer zone" PCR range with no regime at all -- if a roll
happened to land exactly while PCR was sitting in a buffer zone,
re-deriving the regime from PCR would wrongly leave the strategy flat
after closing the old legs instead of reopening the same position type.

Entry: 9:45 AM IST onward. Force-close: 3:00 PM IST, no exceptions.
Size: 10 lots per leg, every position type (Instrument.lot_size used when
present; DEFAULT_LOT_SIZE is only a fallback -- confirm the real NSE
circular value before relying on it for live capital).
Expiry: current week's weekly expiry; rolls to next week's contract on
the expiry date itself.

Display-only fields: each opened position also records its own exit
rules -- pcr_exit_below / pcr_exit_above (pcr_exit_band(), the same
thresholds the PCR exit check itself uses), roll_trigger and exit_time --
so the Paper Trading card can show how close PCR, spot and the clock are
to closing or rolling it. Nothing in evaluate() reads them back.

Runs through services/paper_trading/native_runner.py -- same ctx-based
contract as nifty_pcr_credit_spread.py (see that file for the underlying
NativeContext API this relies on: ctx.state, ctx.now, ctx.get_price,
ctx.get_pcr, ctx.list_weekly_expiries, ctx.find_option, ctx.open_leg,
ctx.close_leg, ctx.record_trade, ctx.note).
"""

import uuid
from datetime import datetime, time as dtime

from sqlalchemy import select

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import IST

STRIKE_STEP = 100
OTM_OFFSET = 200            # bullish/bearish short-leg distance from ATM; their hedge leg is a further OTM_OFFSET beyond that (sideways has no hedge leg -- see leg_specs_for)
LOTS_PER_LEG = 10
DEFAULT_LOT_SIZE = 65       # fallback only -- real Instrument.lot_size is used when present
ENTRY_TIME = dtime(9, 45)
EXIT_TIME = dtime(15, 0)
UNDERLYING_SYMBOL = "NIFTY 50"
PCR_TIMEFRAME = "15m"

ROLL_TRIGGER = 100  # uniform roll distance for all three position types

SIDEWAYS_LOW = 0.80
SIDEWAYS_HIGH = 1.20
BEARISH_ENTRY = 0.75
BULLISH_ENTRY = 1.25
BEARISH_EXIT = 0.80
BULLISH_EXIT = 1.20
SIDEWAYS_EXIT_LOW = 0.75
SIDEWAYS_EXIT_HIGH = 1.25


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


def determine_regime(pcr: float) -> str:
    """"buffer" means a hysteresis gap -- no edge either way, stay flat if not already positioned."""
    if SIDEWAYS_LOW <= pcr <= SIDEWAYS_HIGH:
        return "sideways"
    if pcr > BULLISH_ENTRY:
        return "bullish"
    if pcr < BEARISH_ENTRY:
        return "bearish"
    return "buffer"


def pcr_exit_band(regime: str) -> tuple[float | None, float | None]:
    """(exit if PCR below, exit if PCR above) for an open position of this
    regime -- None where that side has no exit."""
    if regime == "sideways":
        return SIDEWAYS_EXIT_LOW, SIDEWAYS_EXIT_HIGH
    if regime == "bullish":
        return BULLISH_EXIT, None
    if regime == "bearish":
        return None, BEARISH_EXIT
    raise ValueError(f"no exit band for regime '{regime}'")


def in_entry_window(t: dtime) -> bool:
    return ENTRY_TIME <= t < EXIT_TIME


def past_exit_cutoff(t: dtime) -> bool:
    return t >= EXIT_TIME


def leg_specs_for(regime: str, atm: float) -> dict:
    """regime -> {leg_name: (strike, option_type, side)}"""
    if regime == "sideways":
        # Naked short straddle -- no hedge wings (removed on request; see
        # module docstring's SIDEWAYS section for the risk tradeoff).
        return {
            "short_ce": (atm, "CE", "sell"),
            "short_pe": (atm, "PE", "sell"),
        }
    if regime == "bullish":
        return {
            "short_pe": (atm - OTM_OFFSET, "PE", "sell"),
            "long_pe": (atm - OTM_OFFSET * 2, "PE", "buy"),
        }
    if regime == "bearish":
        return {
            "short_ce": (atm + OTM_OFFSET, "CE", "sell"),
            "long_ce": (atm + OTM_OFFSET * 2, "CE", "buy"),
        }
    raise ValueError(f"no legs for regime '{regime}'")


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
            close_side = "buy" if leg["side"] == "sell" else "sell"
            await ctx.close_leg(instrument, close_side, leg["quantity"], exit_price)

            # Short leg profits as price falls, long leg profits as price rises --
            # same sign convention as nifty_pcr_credit_spread.py's close_position.
            leg_pnl = (
                (leg["entry_price"] - exit_price) if leg["side"] == "sell" else (exit_price - leg["entry_price"])
            ) * leg["quantity"]
            total_pnl += leg_pnl
            total_entry_notional += leg["entry_price"] * leg["quantity"]
            trade_legs.append({
                "instrument_id": leg["instrument_id"], "side": "short" if leg["side"] == "sell" else "long",
                "quantity": leg["quantity"], "entry_price": leg["entry_price"], "exit_price": exit_price,
            })

        pnl_pct = (total_pnl / total_entry_notional * 100) if total_entry_notional else 0.0
        await ctx.record_trade(
            legs=trade_legs, pnl=total_pnl, pnl_pct=pnl_pct, exit_reason=exit_reason,
            opened_at=datetime.fromisoformat(position["opened_at"]),
        )
        ctx.state["position"] = None

    is_rollover = False
    regime = None

    if position is not None:
        regime = position["regime"]

        if force_exit:
            await close_position("manual")
            ctx.note("exited", signal="COVER", reason="manual exit")
            return

        if past_exit_cutoff(now_ist.time()):
            await close_position("time_cutoff_3pm")
            ctx.note("exited", signal="COVER", reason="3:00pm IST cutoff")
            return

        pcr = await ctx.get_pcr(timeframe=PCR_TIMEFRAME)
        if pcr is not None:
            exit_below, exit_above = pcr_exit_band(regime)
            pcr_exit = (exit_below is not None and pcr < exit_below) or (exit_above is not None and pcr > exit_above)
            if pcr_exit:
                await close_position(f"pcr_exit_{pcr:.3f}")
                ctx.note("exited", signal="COVER", reason=f"PCR {pcr:.3f} left the {regime} band")
                return

        entry_spot = position.get("entry_spot")
        moved = abs(spot - entry_spot) if entry_spot is not None else 0.0
        if entry_spot is not None and moved >= ROLL_TRIGGER:
            await close_position("rollover")
            is_rollover = True
            position = None  # fall through to reopen the SAME regime at the new ATM below, same tick
        else:
            ctx.note("hold", reason=f"{regime} position open, spot {spot} vs entry {entry_spot} ({moved:.1f}pt moved)")
            return

    if not in_entry_window(now_ist.time()):
        ctx.note("skipped", reason=f"outside entry window ({ENTRY_TIME}-{EXIT_TIME} IST)")
        return

    pcr = await ctx.get_pcr(timeframe=PCR_TIMEFRAME)
    if pcr is None:
        ctx.note("skipped", reason="no PCR data available yet")
        return

    if not is_rollover:
        regime = determine_regime(pcr)
        if regime == "buffer":
            ctx.note("skipped", reason=f"PCR {pcr:.3f} in a hysteresis buffer zone -- no edge either way")
            return
    # else: `regime` was already set above from the position this tick just
    # rolled out of -- a roll always reopens the same position type, never
    # re-derived from PCR (see module docstring for why).

    today = now_ist.date()
    expiries = await ctx.list_weekly_expiries(underlying.id, today)
    if not expiries:
        ctx.note("skipped", reason="no live NIFTY option expiries found")
        return
    expiry = expiries[0]
    if today == expiry and len(expiries) > 1:
        expiry = expiries[1]  # roll to next week on the expiry date itself

    atm = round_to_nearest_100(spot)
    specs = leg_specs_for(regime, atm)

    legs = {}
    for name, (strike, option_type, side) in specs.items():
        instrument = await ctx.find_option(underlying.id, expiry, strike, option_type)
        if instrument is None:
            ctx.note("skipped", reason=f"option contract not found for {expiry} {strike}{option_type}")
            return
        price = await ctx.get_price(instrument.id)
        if price is None:
            ctx.note("skipped", reason=f"no live premium yet for {strike}{option_type}")
            return
        legs[name] = {"instrument": instrument, "strike": strike, "option_type": option_type, "side": side, "price": price}

    lot_size = next(iter(legs.values()))["instrument"].lot_size or DEFAULT_LOT_SIZE
    quantity = float(LOTS_PER_LEG * lot_size)

    for leg in legs.values():
        await ctx.open_leg(leg["instrument"], leg["side"], quantity, leg["price"])

    exit_below, exit_above = pcr_exit_band(regime)
    ctx.state["position"] = {
        "regime": regime,
        "pcr_at_entry": pcr,
        "entry_spot": spot,
        "expiry": expiry.isoformat(),
        # Display-only (see module docstring) -- never read back by evaluate().
        "pcr_exit_below": exit_below,
        "pcr_exit_above": exit_above,
        "roll_trigger": ROLL_TRIGGER,
        "exit_time": EXIT_TIME.strftime("%H:%M"),
        "legs": {
            name: {
                "instrument_id": str(leg["instrument"].id), "strike": leg["strike"], "option_type": leg["option_type"],
                "side": leg["side"], "quantity": quantity, "entry_price": leg["price"],
            }
            for name, leg in legs.items()
        },
        "opened_at": now_utc.isoformat(),
    }
    ctx.note(
        "entered", signal=regime.upper(),
        reason=f"{regime} position opened: " + ", ".join(f"{l['side']} {l['strike']}{l['option_type']}" for l in legs.values()),
    )
