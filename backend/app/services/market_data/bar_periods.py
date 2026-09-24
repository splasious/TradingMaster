"""When a candle is finished. A candle's `ts` is when its period opens; its
OHLC is only final once `ts + duration` has passed -- before that, `close`
is just the latest trade so far. Kite's historical endpoint returns the
current, still-forming candle along with the finished ones, so anything
that stores or signals on candles has to tell the two apart: a forming
candle saved once and never updated keeps whatever price it had at that
moment, and an indicator crossing on one can un-cross a minute later.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.market_data import OhlcvCandle

BAR_DURATIONS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "60m": timedelta(minutes=60),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1wk": timedelta(weeks=1),
}
INTRADAY_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "60m", "4h"})


def is_complete(ts: datetime, timeframe: str, now: datetime) -> bool:
    """True once the candle opening at `ts` has closed. A timeframe with no
    fixed length (e.g. "1mo") counts as complete -- no worse than before."""
    duration = BAR_DURATIONS.get(timeframe)
    return duration is None or as_aware_utc(ts) + duration <= as_aware_utc(now)


async def load_closed_candles(
    db: AsyncSession, instrument_id: uuid.UUID, timeframe: str, limit: int, now: datetime,
) -> list[dict]:
    """The `limit` most recent finished candles at `now`, oldest first, as
    {"ts", "open", "high", "low", "close", "volume"} dicts with UTC `ts`."""
    rows = (
        await db.execute(
            select(OhlcvCandle)
            .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == timeframe, OhlcvCandle.ts <= now)
            .order_by(OhlcvCandle.ts.desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    bars = [
        {"ts": as_aware_utc(c.ts), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
        for c in reversed(rows)
        if is_complete(c.ts, timeframe, now)
    ]
    return bars[-limit:]
