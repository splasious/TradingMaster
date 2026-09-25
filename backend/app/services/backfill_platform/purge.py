"""Deletes every saved candle of a timeframe the app no longer keeps
(bf_settings.purge_timeframes -- 1-minute since 26 Sep 2026), from both
candle tables and bf_coverage.

Runs in the background after startup, one symbol / instrument at a time
(each delete uses the (symbol, timeframe, ts) unique index), so the app
stays responsive while millions of rows go. The timeframe leaves the list
once its candles are gone; a restart midway just carries on.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfCoverage, BfOhlcvBar
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform.coverage import get_settings

logger = logging.getLogger(__name__)

# Between deletes, so the purge never crowds out the app's own queries.
PAUSE_SECONDS = 0.05


class TimeframePurge:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.deleted_backfill_bars = 0
        self.deleted_chart_candles = 0
        self.last_error: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def run(self) -> None:
        try:
            async with AsyncSessionLocal() as db:
                timeframes = list((await get_settings(db)).purge_timeframes or [])
            if not timeframes:
                return
            self.started_at = datetime.now(timezone.utc)
            for timeframe in timeframes:
                await self._purge(timeframe)
            async with AsyncSessionLocal() as db:
                settings = await get_settings(db)
                settings.purge_timeframes = [tf for tf in (settings.purge_timeframes or []) if tf not in timeframes]
                await db.commit()
            self.finished_at = datetime.now(timezone.utc)
            logger.info(
                "Purged %s candles: %d backfill bars, %d chart candles",
                ",".join(timeframes), self.deleted_backfill_bars, self.deleted_chart_candles,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Timeframe purge failed")
            self.last_error = str(exc)

    async def _purge(self, timeframe: str) -> None:
        async with AsyncSessionLocal() as db:
            symbol_ids = (
                await db.execute(select(BfOhlcvBar.symbol_id).where(BfOhlcvBar.timeframe == timeframe).distinct())
            ).scalars().all()
        for symbol_id in symbol_ids:
            async with AsyncSessionLocal() as db:
                result = await db.execute(delete(BfOhlcvBar).where(BfOhlcvBar.symbol_id == symbol_id, BfOhlcvBar.timeframe == timeframe))
                await db.execute(delete(BfCoverage).where(BfCoverage.symbol_id == symbol_id, BfCoverage.timeframe == timeframe))
                await db.commit()
            self.deleted_backfill_bars += result.rowcount or 0
            await asyncio.sleep(PAUSE_SECONDS)

        async with AsyncSessionLocal() as db:
            instrument_ids = (
                await db.execute(select(OhlcvCandle.instrument_id).where(OhlcvCandle.timeframe == timeframe).distinct())
            ).scalars().all()
        for instrument_id in instrument_ids:
            async with AsyncSessionLocal() as db:
                result = await db.execute(delete(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == timeframe))
                await db.commit()
            self.deleted_chart_candles += result.rowcount or 0
            await asyncio.sleep(PAUSE_SECONDS)

        async with AsyncSessionLocal() as db:
            await db.execute(delete(BfCoverage).where(BfCoverage.timeframe == timeframe))
            await db.commit()


timeframe_purge = TimeframePurge()
