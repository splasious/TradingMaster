"""Continuously syncs newly-backfilled bars from the Data Backfill
Platform's isolated bf_* schema into the main Instrument/OhlcvCandle
schema, so Charts/Strategy Builder/Backtesting/Optimization pick up real
backfilled data automatically, not just via the manual "Sync" buttons.
Mirrors the shape of the other background schedulers in this app
(PaperTradingScheduler, BfLiveSyncScheduler): a periodic asyncio task,
started/stopped from main.py's lifespan.

Efficient by design: each tick only looks at symbols with a completed
backfill job newer than their last sync (or never synced at all) -- an
already-synced, stable symbol costs one cheap join per cycle, not a full
bar rescan. Processes a capped batch per tick so one huge "Backfill All"
burst doesn't create a giant transaction or starve the other schedulers
of DB time; a large backlog just drains over a few extra ticks.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfSymbol
from app.services.backfill_platform.catalog_sync import CatalogSyncError, sync_symbol_to_catalog

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 30
MAX_SYMBOLS_PER_TICK = 100


class CatalogSyncScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_run_at: datetime | None = None
        self.last_synced_symbols: int = 0
        self.last_synced_bars: int = 0
        self.last_error: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
            try:
                symbols, bars = await self.sync_pending()
                self.last_synced_symbols = symbols
                self.last_synced_bars = bars
                self.last_run_at = datetime.now(timezone.utc)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Catalog sync tick failed")
                self.last_error = str(exc)

    async def sync_pending(self) -> tuple[int, int]:
        async with AsyncSessionLocal() as db:
            candidates = await self._find_symbols_needing_sync(db)
            synced_symbols = 0
            synced_bars = 0
            now = datetime.now(timezone.utc)
            for symbol in candidates:
                try:
                    result = await sync_symbol_to_catalog(db, symbol)
                    synced_symbols += 1
                    synced_bars += result.bars_synced
                except CatalogSyncError:
                    # No main-catalog mapping for this source (e.g. Zerodha) --
                    # mark synced anyway so it isn't retried every tick.
                    pass
                symbol.last_synced_at = now
            await db.commit()
            return synced_symbols, synced_bars

    async def _find_symbols_needing_sync(self, db: AsyncSession) -> list[BfSymbol]:
        latest_completion = (
            select(BfBackfillJob.symbol_id, func.max(BfBackfillJob.completed_at).label("latest_completed_at"))
            .where(BfBackfillJob.status == BfBackfillStatus.COMPLETED.value)
            .group_by(BfBackfillJob.symbol_id)
            .subquery()
        )
        stmt = (
            select(BfSymbol)
            .join(latest_completion, latest_completion.c.symbol_id == BfSymbol.id)
            .where(
                (BfSymbol.last_synced_at.is_(None))
                | (latest_completion.c.latest_completed_at > BfSymbol.last_synced_at)
            )
            .limit(MAX_SYMBOLS_PER_TICK)
        )
        return (await db.execute(stmt)).scalars().all()


catalog_sync_scheduler = CatalogSyncScheduler()
