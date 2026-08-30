"""A real, always-running background sync loop for the Data Backfill
Platform -- not literal tick-by-tick (neither Yahoo nor this codebase has
a tick feed; see the honesty note below), but a genuine periodic REST
poll against each source's real API, run continuously while the backend
process is up, mirroring paper_trading/scheduler.py's established
asyncio-task pattern.

What "live" actually means per source, honestly:
  - Delta (RWA tokens): polls real 1-minute candles every tick -- Delta's
    RWA/crypto markets trade continuously, no session gate needed.
  - Yahoo (NSE): re-pulls from nse-yahoo-data (which runs its own
    scheduler independently) only during real NSE market hours
    (weekdays 09:15-15:30 IST, not holiday-aware -- same documented
    heuristic used elsewhere in this codebase). Yahoo/yfinance has no
    true tick feed at any granularity; this is a periodic refresh of
    whatever nse-yahoo-data has already accumulated, not a live stream.
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
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.market_data.hours import nse_market_open

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 60


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
        market_open = nse_market_open(now)
        synced = 0
        async with AsyncSessionLocal() as db:
            symbols = (await db.execute(select(BfSymbol))).scalars().all()
            for symbol in symbols:
                try:
                    if symbol.source == "delta":
                        await self._sync_symbol(db, symbol, DeltaExchangeDataSource(), "1m", now)
                        synced += 1
                    elif symbol.source == "yahoo" and market_open:
                        from app.services.market_data.yahoo_source import YahooNSEDataSource

                        await self._sync_symbol(db, symbol, YahooNSEDataSource(), "1d", now)
                        synced += 1
                except MarketDataSourceError:
                    continue  # one symbol's source hiccup shouldn't kill the whole tick
                except Exception:
                    logger.exception("Live sync failed for %s:%s", symbol.source, symbol.symbol)
        return synced

    async def _sync_symbol(self, db, symbol: BfSymbol, data_source, timeframe: str, now: datetime) -> None:
        start = now - timedelta(days=2)
        bars = await data_source.get_historical_data(symbol.symbol, timeframe, start, now)
        if not bars:
            return
        existing_result = await db.execute(
            select(BfOhlcvBar.ts).where(BfOhlcvBar.symbol_id == symbol.id, BfOhlcvBar.timeframe == timeframe)
        )
        existing_ts = {as_aware_utc(ts) for ts in existing_result.scalars().all()}
        for bar in bars:
            bar_ts = as_aware_utc(bar["ts"])
            if bar_ts in existing_ts:
                continue
            db.add(
                BfOhlcvBar(
                    symbol_id=symbol.id, timeframe=timeframe, ts=bar_ts,
                    open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"], volume=bar.get("volume"),
                )
            )
            existing_ts.add(bar_ts)
        await db.commit()


bf_live_sync_scheduler = BfLiveSyncScheduler()
