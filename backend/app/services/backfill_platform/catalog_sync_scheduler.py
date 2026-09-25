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
from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfOhlcvBar, BfSymbol
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
        # Symbols the latest tick couldn't sync (the rest were saved).
        self.last_failures: list[str] = []

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
                self.last_error = (
                    f"{len(self.last_failures)} symbol(s) failed to sync: " + "; ".join(self.last_failures[:5])
                    if self.last_failures else None
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Catalog sync tick failed")
                self.last_error = str(exc)

    async def sync_pending(self) -> tuple[int, int]:
        """Each symbol is copied and committed on its own. They used to
        share one transaction, so a single symbol that failed to save rolled
        back the whole batch of up to 100 -- and the same batch came back
        every tick, stalling everything behind it. A failing symbol is now
        rolled back, logged and retried next tick; the rest carry on."""
        async with AsyncSessionLocal() as db:
            candidate_ids = [symbol.id for symbol in await self._find_symbols_needing_sync(db)]
            synced_symbols = 0
            synced_bars = 0
            failures: list[str] = []
            for symbol_id in candidate_ids:
                symbol = await db.get(BfSymbol, symbol_id)
                if symbol is None:
                    continue
                name = symbol.symbol
                try:
                    result = await sync_symbol_to_catalog(db, symbol)
                    synced_symbols += 1
                    synced_bars += result.bars_synced
                except CatalogSyncError:
                    # No main-catalog mapping for this source -- every real
                    # source has one today, so this only fires for a source
                    # added to _VALID_SOURCES without a matching catalog_sync
                    # entry. Mark synced anyway so it isn't retried every tick.
                    pass
                except Exception as exc:
                    await db.rollback()
                    logger.exception("Catalog sync failed for %s", name)
                    failures.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
                    continue
                symbol.last_synced_at = datetime.now(timezone.utc)
                await db.commit()
            self.last_failures = failures
            return synced_symbols, synced_bars

    async def _find_symbols_needing_sync(self, db: AsyncSession) -> list[BfSymbol]:
        """A symbol whose bars changed since it was last copied: a job saved
        bars after that -- completed, or failed partway after saving some --
        or it has bars but was never copied at all (e.g. a Delta symbol
        filled only by the live sync, which has no jobs)."""
        latest_save = (
            select(BfBackfillJob.symbol_id, func.max(BfBackfillJob.completed_at).label("latest_completed_at"))
            .where((BfBackfillJob.status == BfBackfillStatus.COMPLETED.value) | (BfBackfillJob.inserted_count > 0))
            .group_by(BfBackfillJob.symbol_id)
            .subquery()
        )
        has_bars = select(BfOhlcvBar.id).where(BfOhlcvBar.symbol_id == BfSymbol.id).exists()
        stmt = (
            select(BfSymbol)
            .outerjoin(latest_save, latest_save.c.symbol_id == BfSymbol.id)
            .where(
                (latest_save.c.latest_completed_at.is_not(None) & BfSymbol.last_synced_at.is_(None))
                | (latest_save.c.latest_completed_at > BfSymbol.last_synced_at)
                | (latest_save.c.latest_completed_at.is_(None) & BfSymbol.last_synced_at.is_(None) & has_bars)
            )
            .order_by(latest_save.c.latest_completed_at.nulls_last())
            .limit(MAX_SYMBOLS_PER_TICK)
        )
        return (await db.execute(stmt)).scalars().all()


catalog_sync_scheduler = CatalogSyncScheduler()
