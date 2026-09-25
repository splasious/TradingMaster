"""A real, always-running background sync loop for the Data Backfill
Platform -- not literal tick-by-tick (this codebase has no tick feed; see
the honesty note below), but a genuine periodic REST poll against each
source's real API, run continuously while the backend process is up,
mirroring paper_trading/scheduler.py's established asyncio-task pattern.

What "live" actually means per source, honestly:
  - Delta (RWA tokens): polls real 1-minute candles every tick -- Delta's
    RWA/crypto markets trade continuously, no session gate needed.
  - Zerodha: NOT run here. Kite's historical/LTP endpoints need a
    specific user's authenticated session, and there's no single "the"
    background session to run this under -- users sync that source on
    demand from the UI instead of a silent background job risking a
    stale/expired token failing unattended.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.services.backfill_platform.catalog_sync import sync_symbol_to_catalog
from app.services.backfill_platform.jobs import save_bars
from app.services.market_data.bar_periods import is_complete
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 60
# A candle counts as finished this long after it closes, giving the source
# time to settle its final values (as active_timeframe_sync_scheduler does).
_SETTLE = timedelta(seconds=10)


class BfLiveSyncScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_sync_at: datetime | None = None
        self.last_error: str | None = None
        self.last_synced_count: int = 0

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
                self.last_synced_count = await self._sync_once()
                self.last_sync_at = datetime.now(timezone.utc)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Live sync tick failed")
                self.last_error = str(exc)

    async def _sync_once(self) -> int:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            targets = (await db.execute(select(BfSymbol.id, BfSymbol.symbol).where(BfSymbol.source == "delta"))).all()
        synced = 0
        for symbol_id, name in targets:
            # A session per symbol: a failure rolls back that symbol's writes
            # only, instead of leaving one shared session unusable (or
            # committing them along with the next symbol's).
            async with AsyncSessionLocal() as db:
                try:
                    symbol = await db.get(BfSymbol, symbol_id)
                    if symbol is None:
                        continue
                    await self._sync_symbol(db, symbol, DeltaExchangeDataSource(), "1m", now)
                    synced += 1
                except MarketDataSourceError:
                    continue  # one symbol's source hiccup shouldn't kill the whole tick
                except Exception:
                    logger.exception("Live sync failed for delta:%s", name)
        return synced

    async def _sync_symbol(self, db, symbol: BfSymbol, data_source, timeframe: str, now: datetime) -> None:
        """Saves the finished candles of the last two days not stored yet,
        then copies them on to the main candle table if the symbol is
        already there. The minute still in progress is left for a later
        tick: saved now it would keep its partial values for good, since a
        stored bar is never overwritten."""
        start = now - timedelta(days=2)
        bars = await data_source.get_historical_data(symbol.symbol, timeframe, start, now)
        bars = [bar for bar in bars if is_complete(bar["ts"], timeframe, now - _SETTLE)]
        if not bars:
            return
        existing_result = await db.execute(
            select(BfOhlcvBar.ts).where(
                BfOhlcvBar.symbol_id == symbol.id, BfOhlcvBar.timeframe == timeframe, BfOhlcvBar.ts >= start,
            )
        )
        existing_ts = {as_aware_utc(ts) for ts in existing_result.scalars().all()}
        new_bars = [bar for bar in bars if as_aware_utc(bar["ts"]) not in existing_ts]
        if not new_bars:
            return
        _, inserted = await save_bars(db, symbol.id, timeframe, new_bars)
        # A symbol not in the main catalog yet gets there -- with all its
        # history -- through CatalogSyncScheduler after its first backfill.
        if inserted and symbol.last_synced_at is not None:
            since = min(as_aware_utc(bar["ts"]) for bar in new_bars)
            await sync_symbol_to_catalog(db, symbol, timeframe=timeframe, since=since)
        await db.commit()


bf_live_sync_scheduler = BfLiveSyncScheduler()
