"""Readable closed-trade records for Advanced (native) deployments.

A native strategy's `ctx.record_trade()` only hands over what the strategy
knows when it closes: per-leg instrument_id/side/quantity/entry/exit plus
a P&L. That's enough for cash accounting, but the Closed Trades table
could only print one run-on string per trade ("short NIFTY26SEP23400CE
650@122.60->128.90, short ...") -- no lots, no net premium, no charges, no
net P&L -- and the saved row itself held nothing but instrument ids.

- `resolve_leg_details()` snapshots each leg's contract (symbol, strike,
  CE/PE, expiry, lot size, underlying) onto the leg, at record time for new
  trades and on read for rows saved before this existed.
- `estimate_charges()` is what the round trip would have cost in brokerage
  and statutory levies on NSE/NFO. Paper fills pay none of it: cash and the
  pool's Realized P&L stay gross, and this is shown as an estimate beside
  them.
- `summarize_trade()` is the one-row view (underlying, structure, net
  premium in/out, qty/lots, gross/charges/net) the API returns.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.services.backfill_platform.catalog_sync import UNDERLYING_NAME_ALIASES

IST = timezone(timedelta(hours=5, minutes=30))

# Kite's F&O name for an index underlying ("NIFTY"), not our seeded index
# row's symbol ("NIFTY 50") -- the name a trader reads on a contract note.
_FNO_UNDERLYING_NAMES = {symbol: name for name, symbol in UNDERLYING_NAME_ALIASES.items()}

# Keys resolve_leg_details() fills in from the instrument catalog. A value
# the strategy already put on the leg itself always wins.
_SNAPSHOT_KEYS = ("instrument_symbol", "exchange", "instrument_type", "strike", "option_type", "expiry", "lot_size", "underlying_symbol")


def _parse_uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def resolve_leg_details(db: AsyncSession, trades_legs: list[list[dict]]) -> list[list[dict]]:
    """Copies of each trade's legs with contract details filled in, batched
    into two queries across every trade. A leg whose instrument can't be
    found (a bad id, or a contract since removed from the catalog) keeps
    whatever it already had rather than failing the whole record."""
    instrument_ids = {
        leg_id for legs in trades_legs for leg in legs
        if (leg_id := _parse_uuid(leg.get("instrument_id"))) is not None
    }
    instruments: dict[uuid.UUID, Instrument] = {}
    if instrument_ids:
        result = await db.execute(select(Instrument).where(Instrument.id.in_(instrument_ids)))
        instruments = {i.id: i for i in result.scalars()}
    underlying_ids = {i.underlying_instrument_id for i in instruments.values() if i.underlying_instrument_id} - instruments.keys()
    if underlying_ids:
        result = await db.execute(select(Instrument).where(Instrument.id.in_(underlying_ids)))
        instruments.update({i.id: i for i in result.scalars()})

    resolved: list[list[dict]] = []
    for legs in trades_legs:
        legs_out = []
        for leg in legs:
            leg = dict(leg)
            instrument = instruments.get(_parse_uuid(leg.get("instrument_id")))
            if instrument is not None:
                underlying = instruments.get(instrument.underlying_instrument_id) if instrument.underlying_instrument_id else None
                if underlying is not None:
                    underlying_symbol = _FNO_UNDERLYING_NAMES.get(underlying.symbol, underlying.symbol)
                elif instrument.instrument_type in ("option", "future"):
                    underlying_symbol = None
                else:
                    underlying_symbol = instrument.symbol
                details = {
                    "instrument_symbol": instrument.symbol, "exchange": instrument.exchange,
                    "instrument_type": instrument.instrument_type, "strike": instrument.strike,
                    "option_type": instrument.option_type,
                    "expiry": instrument.expiry.isoformat() if instrument.expiry else None,
                    "lot_size": instrument.lot_size, "underlying_symbol": underlying_symbol,
                }
                for key in _SNAPSHOT_KEYS:
                    if leg.get(key) is None:
                        leg[key] = details[key]
            for key in _SNAPSHOT_KEYS:
                leg.setdefault(key, None)
            legs_out.append(leg)
        resolved.append(legs_out)
    return resolved


@dataclass(frozen=True)
class _ChargeSchedule:
    """Rates in percent of turnover. Brokerage is `brokerage_flat` per
    executed order, or `brokerage_pct` of that order's turnover capped at
    `brokerage_flat` when set (the usual discount-broker "0.03% or Rs 20,
    whichever is lower")."""

    brokerage_flat: float
    brokerage_pct: float | None
    stt_buy_pct: float
    stt_sell_pct: float
    exchange_pct: float
    stamp_buy_pct: float


# NSE schedule as revised from 1 Oct 2024, with discount-broker brokerage.
# Excludes DP charges on delivery sells.
_OPTIONS = _ChargeSchedule(brokerage_flat=20.0, brokerage_pct=None, stt_buy_pct=0.0, stt_sell_pct=0.1, exchange_pct=0.03503, stamp_buy_pct=0.003)
_FUTURES = _ChargeSchedule(brokerage_flat=20.0, brokerage_pct=0.03, stt_buy_pct=0.0, stt_sell_pct=0.02, exchange_pct=0.00173, stamp_buy_pct=0.002)
_EQUITY_INTRADAY = _ChargeSchedule(brokerage_flat=20.0, brokerage_pct=0.03, stt_buy_pct=0.0, stt_sell_pct=0.025, exchange_pct=0.00297, stamp_buy_pct=0.003)
_EQUITY_DELIVERY = _ChargeSchedule(brokerage_flat=0.0, brokerage_pct=None, stt_buy_pct=0.1, stt_sell_pct=0.1, exchange_pct=0.00297, stamp_buy_pct=0.015)
_SEBI_FEE_PCT = 0.0001  # Rs 10 per crore
_GST_PCT = 18.0  # on brokerage + exchange + SEBI fees


def _is_short(leg: dict) -> bool:
    """record_trade's contract says "short"/"long"; a hand-written strategy
    passing its open_leg() spelling ("sell"/"buy") means the same thing."""
    return str(leg.get("side", "")).lower() in ("short", "sell")


def _schedule_for(leg: dict, intraday: bool) -> _ChargeSchedule | None:
    if leg.get("option_type") or leg.get("instrument_type") == "option":
        return _OPTIONS
    if leg.get("instrument_type") == "future":
        return _FUTURES
    if leg.get("exchange") in ("NSE", "BSE") and leg.get("instrument_type") == "equity":
        return _EQUITY_INTRADAY if intraday else _EQUITY_DELIVERY
    return None


def estimate_charges(legs: list[dict], opened_at: datetime, closed_at: datetime) -> float | None:
    """Brokerage + STT + exchange + SEBI + stamp duty + GST for opening
    and closing every leg (two executed orders per leg). None when any leg
    isn't an NSE/BSE/NFO contract this schedule covers (e.g. a Delta
    Exchange perpetual) or its details aren't known -- no charges beats a
    wrong figure. Legs need resolve_leg_details()'s keys."""
    if not legs:
        return None
    intraday = as_aware_utc(opened_at).astimezone(IST).date() == as_aware_utc(closed_at).astimezone(IST).date()
    total = 0.0
    for leg in legs:
        schedule = _schedule_for(leg, intraday)
        if schedule is None:
            return None
        quantity = abs(float(leg["quantity"]))
        entry_turnover = float(leg["entry_price"]) * quantity
        exit_turnover = float(leg["exit_price"]) * quantity
        buy, sell = (exit_turnover, entry_turnover) if _is_short(leg) else (entry_turnover, exit_turnover)

        brokerage = 0.0
        for turnover in (entry_turnover, exit_turnover):
            if schedule.brokerage_pct is None:
                brokerage += schedule.brokerage_flat
            else:
                brokerage += min(schedule.brokerage_flat, turnover * schedule.brokerage_pct / 100)
        stt = (buy * schedule.stt_buy_pct + sell * schedule.stt_sell_pct) / 100
        exchange = (buy + sell) * schedule.exchange_pct / 100
        sebi = (buy + sell) * _SEBI_FEE_PCT / 100
        stamp = buy * schedule.stamp_buy_pct / 100
        gst = (brokerage + exchange + sebi) * _GST_PCT / 100
        total += brokerage + stt + exchange + sebi + stamp + gst
    return round(total, 2)


def _leg_pnl(leg: dict) -> float:
    diff = float(leg["entry_price"]) - float(leg["exit_price"]) if _is_short(leg) else float(leg["exit_price"]) - float(leg["entry_price"])
    return diff * float(leg["quantity"])


def _lots(quantity: float, lot_size) -> float | None:
    return quantity / lot_size if lot_size else None


def _structure(legs: list[dict], is_credit: bool) -> str:
    """A trader's name for the legs: "Short Straddle", "Bull Put Spread",
    "Iron Condor", ... falling back to a plain "Long"/"Short" or "N-leg"."""
    if len(legs) == 1:
        leg = legs[0]
        side = "Short" if leg["side"] == "short" else "Long"
        return f"{side} {leg['option_type']}" if leg.get("option_type") else side

    option_types = [leg.get("option_type") for leg in legs]
    sides = {leg["side"] for leg in legs}
    if len(legs) == 2 and sorted(t or "" for t in option_types) == ["CE", "PE"] and len(sides) == 1:
        side = "Short" if sides == {"short"} else "Long"
        same_strike = legs[0].get("strike") is not None and legs[0].get("strike") == legs[1].get("strike")
        return f"{side} {'Straddle' if same_strike else 'Strangle'}"
    if len(legs) == 2 and option_types[0] and option_types[0] == option_types[1] and sides == {"short", "long"}:
        if option_types[0] == "CE":
            return "Bear Call Spread" if is_credit else "Bull Call Spread"
        return "Bull Put Spread" if is_credit else "Bear Put Spread"
    if len(legs) == 4 and sorted(t or "" for t in option_types) == ["CE", "CE", "PE", "PE"] and sides == {"short", "long"}:
        short_strikes = {leg.get("strike") for leg in legs if leg["side"] == "short"}
        return "Iron Butterfly" if len(short_strikes) == 1 else "Iron Condor"
    return f"{len(legs)}-leg"


def summarize_trade(legs: list[dict]) -> dict:
    """The one-row view of a closed trade, from legs that already carry
    resolve_leg_details()'s keys. Entry/exit are the per-unit net premium
    (sum of short premiums minus long, or the reverse for a net debit) --
    the way a straddle's "238.10 in, 221.75 out" is quoted -- and only
    when every leg traded the same quantity; otherwise per-leg only."""
    legs = [
        {**leg, "side": "short" if _is_short(leg) else "long", "pnl": round(_leg_pnl(leg), 2), "lots": _lots(float(leg["quantity"]), leg.get("lot_size"))}
        for leg in legs
    ]
    if not legs:
        return {"legs": [], "underlying_symbol": None, "structure": None, "side": None, "entry_price": None,
                "exit_price": None, "quantity": None, "lots": None}

    net_entry = sum(float(leg["entry_price"]) * (1 if leg["side"] == "short" else -1) for leg in legs)
    net_exit = sum(float(leg["exit_price"]) * (1 if leg["side"] == "short" else -1) for leg in legs)
    if len(legs) == 1:
        is_credit = legs[0]["side"] == "short"
    else:
        is_credit = net_entry >= 0

    quantities = {float(leg["quantity"]) for leg in legs}
    quantity = quantities.pop() if len(quantities) == 1 else None
    entry_price = exit_price = None
    if quantity is not None:
        if len(legs) == 1:
            entry_price, exit_price = float(legs[0]["entry_price"]), float(legs[0]["exit_price"])
        else:
            sign = 1 if is_credit else -1
            entry_price, exit_price = round(net_entry * sign, 2), round(net_exit * sign, 2)
    lot_sizes = {leg.get("lot_size") for leg in legs}
    lots = _lots(quantity, lot_sizes.pop()) if quantity is not None and len(lot_sizes) == 1 else None

    underlyings = sorted({leg["underlying_symbol"] for leg in legs if leg.get("underlying_symbol")})
    if not underlyings and len(legs) == 1:
        underlyings = [legs[0]["instrument_symbol"]] if legs[0].get("instrument_symbol") else []

    return {
        "legs": legs,
        "underlying_symbol": ", ".join(underlyings) or None,
        "structure": _structure(legs, is_credit),
        "side": "short" if is_credit else "long",
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": quantity,
        "lots": lots,
    }
