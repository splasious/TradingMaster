"""Put-Call Ratio and open-interest-change computation for the Options
Dashboard.

Aggregates real per-strike open interest (see zerodha_broker.py's oi=1
request during NFO backfill) across every option contract of one
underlying + expiry, at each bar timestamp they share -- PCR at time t is
sum(all PE open_interest at t) / sum(all CE open_interest at t), the
standard market-breadth definition, computed here rather than trusted from
any third-party source since it's a straightforward roll-up of data this
app already owns.

"Change in OI" is not a separate Kite field -- it's simply this bar's
total OI minus the previous bar's, computed here per side (call/put)
since that's what a change-in-OI chart plots.
"""

import uuid
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST


async def compute_pcr_series(
    db: AsyncSession, underlying_instrument_id: uuid.UUID, expiry: date, timeframe: str,
) -> list[dict]:
    option_rows = (
        await db.execute(
            select(Instrument.id, Instrument.option_type).where(
                Instrument.underlying_instrument_id == underlying_instrument_id,
                Instrument.expiry == expiry,
                Instrument.instrument_type == "option",
            )
        )
    ).all()
    if not option_rows:
        return []
    ce_ids = {i for i, ot in option_rows if ot == "CE"}
    pe_ids = {i for i, ot in option_rows if ot == "PE"}

    candle_rows = (
        await db.execute(
            select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.open_interest)
            .where(OhlcvCandle.instrument_id.in_(ce_ids | pe_ids), OhlcvCandle.timeframe == timeframe)
            .order_by(OhlcvCandle.ts)
        )
    ).all()

    by_ts: dict[datetime, dict[str, float]] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    for inst_id, ts, oi in candle_rows:
        if oi is None:
            continue
        side = "call" if inst_id in ce_ids else "put"
        by_ts[ts][side] += oi

    series: list[dict] = []
    prev_call: float | None = None
    prev_put: float | None = None
    for ts in sorted(by_ts):
        call_oi = by_ts[ts]["call"]
        put_oi = by_ts[ts]["put"]
        series.append({
            "ts": ts,
            "total_call_oi": call_oi,
            "total_put_oi": put_oi,
            "pcr": (put_oi / call_oi) if call_oi > 0 else None,
            "call_oi_change": (call_oi - prev_call) if prev_call is not None else None,
            "put_oi_change": (put_oi - prev_put) if prev_put is not None else None,
        })
        prev_call, prev_put = call_oi, put_oi
    return series


async def compute_effective_pcr(
    db: AsyncSession, underlying_symbol: str = "NIFTY 50", num_expiries: int = 4, timeframe: str = "15m",
) -> float | None:
    """Single PCR number across the underlying's nearest `num_expiries`
    live option expiries -- put OI and call OI each summed across all of
    them first, then divided once, not an average of 4 separate ratios
    (matches how PCR is conventionally read as a market-breadth number,
    not per-expiry). Used by paper_trading/engine.py to hand a PCR-driven
    Python strategy a single live number each evaluation tick -- the
    sandbox itself has no DB access to compute this on its own (PRD
    Rule: the sandbox subprocess boundary has no network/DB access by
    design, see services/strategy/sandbox.py).

    Default timeframe is "15m", not "1d" -- confirmed against the real
    production database (2026-09-12) that NFO option open-interest is
    only ever backfilled/synced at 15m; a "1d" default here would have
    silently matched zero rows and always returned None."""
    underlying = (await db.execute(select(Instrument).where(Instrument.symbol == underlying_symbol))).scalar_one_or_none()
    if underlying is None:
        return None

    today = datetime.now(timezone.utc).astimezone(IST).date()
    expiry_rows = (
        await db.execute(
            select(Instrument.expiry)
            .where(
                Instrument.underlying_instrument_id == underlying.id,
                Instrument.instrument_type == "option",
                Instrument.expiry.is_not(None),
                Instrument.expiry >= today,
            )
            .distinct()
            .order_by(Instrument.expiry)
            .limit(num_expiries)
        )
    ).scalars().all()
    if not expiry_rows:
        return None

    total_call_oi = 0.0
    total_put_oi = 0.0
    for expiry in expiry_rows:
        series = await compute_pcr_series(db, underlying.id, expiry, timeframe)
        if not series:
            continue
        latest = series[-1]
        total_call_oi += latest["total_call_oi"]
        total_put_oi += latest["total_put_oi"]

    return (total_put_oi / total_call_oi) if total_call_oi > 0 else None
