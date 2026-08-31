"""Shared candle loading for the backtest and optimization runners.

Mirrors what Charts/indicators already do: fetch directly-stored candles
at the requested timeframe, or -- if none are stored at exactly that
timeframe -- resample from the finest stored base timeframe finer-or-equal
to the target. Without this, picking a timeframe on the Backtesting page
that wasn't the exact one backfilled (e.g. only "1d" was backfilled but
"60m" was selected) would silently fail with "not enough candles" even
though real, usable base data exists.
"""

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_data import OhlcvCandle
from app.services.market_data.resample import resample_candles

_TIMEFRAME_ORDER = ["1m", "5m", "15m", "30m", "60m", "4h", "1d", "1wk", "1mo"]


def _date_filters(stmt, start_date: date | None, end_date: date | None):
    if start_date:
        stmt = stmt.where(OhlcvCandle.ts >= datetime.combine(start_date, time.min, tzinfo=timezone.utc))
    if end_date:
        stmt = stmt.where(OhlcvCandle.ts <= datetime.combine(end_date, time.max, tzinfo=timezone.utc))
    return stmt


async def _fetch(db: AsyncSession, instrument_id: uuid.UUID, timeframe: str, start_date: date | None, end_date: date | None) -> list[OhlcvCandle]:
    stmt = select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == timeframe).order_by(OhlcvCandle.ts)
    stmt = _date_filters(stmt, start_date, end_date)
    return list((await db.execute(stmt)).scalars().all())


async def load_candles(
    db: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[OhlcvCandle]:
    direct = await _fetch(db, instrument_id, timeframe, start_date, end_date)
    if direct:
        return direct

    if timeframe not in _TIMEFRAME_ORDER:
        return []
    target_idx = _TIMEFRAME_ORDER.index(timeframe)

    available = (
        await db.execute(select(OhlcvCandle.timeframe).distinct().where(OhlcvCandle.instrument_id == instrument_id))
    ).scalars().all()
    base_timeframe = None
    for tf in available:
        if tf not in _TIMEFRAME_ORDER:
            continue
        idx = _TIMEFRAME_ORDER.index(tf)
        if idx <= target_idx and (base_timeframe is None or idx > _TIMEFRAME_ORDER.index(base_timeframe)):
            base_timeframe = tf
    if base_timeframe is None:
        return []

    base_candles = await _fetch(db, instrument_id, base_timeframe, start_date, end_date)
    if not base_candles:
        return []

    bars = [
        {"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
        for c in base_candles
    ]
    try:
        resampled = resample_candles(bars, timeframe)
    except ValueError:
        return []

    # Unsaved OhlcvCandle instances -- never added to the session, just
    # reused as attribute-compatible in-memory stand-ins so the rest of
    # the backtest/optimization pipeline (which expects ORM-shaped
    # candles) doesn't need to know these were derived, not stored.
    return [
        OhlcvCandle(
            instrument_id=instrument_id, timeframe=timeframe, ts=bar["ts"],
            open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"], volume=bar["volume"],
            source="resampled",
        )
        for bar in resampled
    ]
