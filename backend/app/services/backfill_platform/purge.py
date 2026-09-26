"""Deletes saved candles the app no longer keeps, from both candle tables
and bf_coverage:
  - every candle of a timeframe in bf_settings.purge_timeframes (1-minute
    since 26 Sep 2026);
  - every candle of a stock option when bf_settings.purge_stock_options is
    set (26 Sep 2026: they aren't downloaded any more, see topup.py). Index
    options, futures and the contract list itself are kept.

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
from app.models.backfill_platform import BfCoverage, BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform.coverage import get_settings
from app.services.backfill_platform.topup import is_stock_option

logger = logging.getLogger(__name__)

# Between deletes, so the purge never crowds out the app's own queries.
PAUSE_SECONDS = 0.05
# Stock options are deleted this many contracts per statement.
CONTRACT_BATCH = 50


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
                settings = await get_settings(db)
                timeframes = list(settings.purge_timeframes or [])
                stock_options = settings.purge_stock_options
            if not timeframes and not stock_options:
                return
            self.started_at = datetime.now(timezone.utc)
            for timeframe in timeframes:
                await self._purge(timeframe)
            if stock_options:
                await self._purge_stock_options()
            async with AsyncSessionLocal() as db:
                settings = await get_settings(db)
                settings.purge_timeframes = [tf for tf in (settings.purge_timeframes or []) if tf not in timeframes]
                settings.purge_stock_options = False
                await db.commit()
            self.finished_at = datetime.now(timezone.utc)
            logger.info(
                "Purged %s candles: %d backfill bars, %d chart candles",
                ",".join(timeframes + (["stock options"] if stock_options else [])),
                self.deleted_backfill_bars, self.deleted_chart_candles,
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

    async def _purge_stock_options(self) -> None:
        async with AsyncSessionLocal() as db:
            symbols = (
                await db.execute(select(BfSymbol.id, BfSymbol.symbol).where(BfSymbol.source == "zerodha_nfo", is_stock_option()))
            ).all()
        for i in range(0, len(symbols), CONTRACT_BATCH):
            ids = [s.id for s in symbols[i : i + CONTRACT_BATCH]]
            async with AsyncSessionLocal() as db:
                result = await db.execute(delete(BfOhlcvBar).where(BfOhlcvBar.symbol_id.in_(ids)))
                await db.execute(delete(BfCoverage).where(BfCoverage.symbol_id.in_(ids)))
                await db.commit()
            self.deleted_backfill_bars += result.rowcount or 0
            await asyncio.sleep(PAUSE_SECONDS)

        tradingsymbols = [s.symbol for s in symbols]
        for i in range(0, len(tradingsymbols), CONTRACT_BATCH):
            async with AsyncSessionLocal() as db:
                instrument_ids = (
                    await db.execute(
                        select(Instrument.id).where(
                            Instrument.exchange == "NFO", Instrument.instrument_type == "option",
                            Instrument.external_ref.in_(tradingsymbols[i : i + CONTRACT_BATCH]),
                        )
                    )
                ).scalars().all()
                if not instrument_ids:
                    continue
                result = await db.execute(delete(OhlcvCandle).where(OhlcvCandle.instrument_id.in_(instrument_ids)))
                await db.commit()
            self.deleted_chart_candles += result.rowcount or 0
            await asyncio.sleep(PAUSE_SECONDS)


timeframe_purge = TimeframePurge()
