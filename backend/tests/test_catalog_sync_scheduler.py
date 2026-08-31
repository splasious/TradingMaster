from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform.catalog_sync_scheduler import CatalogSyncScheduler


async def _completed_job(db_session: AsyncSession, symbol: BfSymbol, completed_at: datetime) -> BfBackfillJob:
    job = BfBackfillJob(
        symbol_id=symbol.id, source=symbol.source, timeframe="1d",
        status=BfBackfillStatus.COMPLETED.value, completed_at=completed_at,
    )
    db_session.add(job)
    return job


async def test_scheduler_syncs_symbol_never_synced_before(db_session: AsyncSession):
    symbol = BfSymbol(source="yahoo", symbol="SCHEDNEVER", display_name="Sched Never Co")
    db_session.add(symbol)
    await db_session.flush()
    await _completed_job(db_session, symbol, datetime.now(timezone.utc))
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()

    scheduler = CatalogSyncScheduler()
    candidates = await scheduler._find_symbols_needing_sync(db_session)

    assert any(c.id == symbol.id for c in candidates)


async def test_scheduler_skips_symbol_synced_after_its_last_completed_job(db_session: AsyncSession):
    symbol = BfSymbol(source="yahoo", symbol="SCHEDUPTODATE", display_name="Up To Date Co", last_synced_at=datetime.now(timezone.utc))
    db_session.add(symbol)
    await db_session.flush()
    await _completed_job(db_session, symbol, datetime.now(timezone.utc) - timedelta(hours=1))
    await db_session.commit()

    scheduler = CatalogSyncScheduler()
    candidates = await scheduler._find_symbols_needing_sync(db_session)

    assert not any(c.id == symbol.id for c in candidates)


async def test_scheduler_resyncs_symbol_with_newer_completed_job_than_last_sync(db_session: AsyncSession):
    old_sync = datetime.now(timezone.utc) - timedelta(hours=2)
    symbol = BfSymbol(source="delta", symbol="SCHEDSTALE", display_name="Stale Co", last_synced_at=old_sync)
    db_session.add(symbol)
    await db_session.flush()
    await _completed_job(db_session, symbol, datetime.now(timezone.utc))  # newer than last_synced_at
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()

    scheduler = CatalogSyncScheduler()
    candidates = await scheduler._find_symbols_needing_sync(db_session)

    assert any(c.id == symbol.id for c in candidates)


async def test_sync_pending_actually_syncs_and_marks_last_synced_at(db_session: AsyncSession):
    symbol = BfSymbol(source="yahoo", symbol="SCHEDREAL", display_name="Real Sync Co")
    db_session.add(symbol)
    await db_session.flush()
    await _completed_job(db_session, symbol, datetime.now(timezone.utc))
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()

    scheduler = CatalogSyncScheduler()
    from app.services.backfill_platform import catalog_sync_scheduler as module

    original = module.AsyncSessionLocal
    try:
        module.AsyncSessionLocal = lambda: _SessionCtx(db_session)
        synced_symbols, synced_bars = await scheduler.sync_pending()
    finally:
        module.AsyncSessionLocal = original

    assert synced_symbols == 1
    assert synced_bars == 1

    refreshed = (await db_session.execute(select(BfSymbol).where(BfSymbol.id == symbol.id))).scalar_one()
    assert refreshed.last_synced_at is not None

    instrument = (await db_session.execute(select(Instrument).where(Instrument.symbol == "SCHEDREAL"))).scalar_one()
    candles = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id))).scalars().all()
    assert len(candles) == 1


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> None:
        return None
