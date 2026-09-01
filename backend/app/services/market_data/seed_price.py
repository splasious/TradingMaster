import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_data import OhlcvCandle


async def get_seed_price(db: AsyncSession, instrument_id: uuid.UUID) -> float:
    """Starting price for a freshly tick_engine-subscribed instrument: its
    most recent stored candle close, so the simulated random-walk fallback
    (used until RealPriceFeed's next poll lands) starts near the real
    price instead of an arbitrary constant. 100.0 only when this instrument
    has no candle history at all yet."""
    result = await db.execute(
        select(OhlcvCandle.close).where(OhlcvCandle.instrument_id == instrument_id).order_by(OhlcvCandle.ts.desc()).limit(1)
    )
    row = result.first()
    return float(row[0]) if row else 100.0
