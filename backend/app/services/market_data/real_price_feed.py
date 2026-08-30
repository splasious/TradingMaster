"""Feeds `TickEngine` with genuine prices polled from Yahoo Finance (NSE,
via nse-yahoo-data) and Delta Exchange's real public ticker API, for
whichever instruments currently have subscribers on the Markets page
WebSocket. Mirrors the Data Backfill Platform's live sync scheduler pattern
(same asyncio-task shape), but writes into TickEngine instead of the bf_*
tables so the existing Markets WS protocol and paper-trading fills pick up
real prices with no other code changes.

Real, not tick-by-tick: refreshed on a periodic REST poll
(REFRESH_INTERVAL_SECONDS), not a push/streaming feed -- no source used
here offers one. Delta (RWA tokens only -- crypto is deliberately excluded,
per the same instruction that scoped the Data Backfill Platform's Delta
block) is polled continuously; Yahoo/NSE only during real market hours,
since nse-yahoo-data has nothing new to report outside them anyway.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.market_data.hours import nse_market_open
from app.services.market_data.tick_engine import TickEngine, tick_engine
from app.services.market_data.yahoo_source import YahooNSEDataSource

logger = logging.getLogger(__name__)
REFRESH_INTERVAL_SECONDS = 15


async def fetch_real_price(instrument: Instrument, *, market_open: bool | None = None) -> tuple[float, str] | None:
    """One real price lookup for a single instrument, or None if this
    instrument has no real source mapped (or the source has nothing new
    right now, e.g. NSE outside market hours on a first-ever fetch)."""
    if instrument.data_source == "delta_exchange":
        ticker = await DeltaExchangeDataSource().get_ticker(instrument.external_ref)
        return ticker["price"], "delta"
    if instrument.data_source == "yahoo_nse":
        if market_open is None:
            market_open = nse_market_open(datetime.now(timezone.utc))
        if not market_open:
            return None
        end = datetime.now(timezone.utc)
        bars = await YahooNSEDataSource().get_historical_data(instrument.external_ref, "1m", end - timedelta(hours=6), end)
        if bars:
            return bars[-1]["close"], "yahoo"
        return None
    return None


class RealPriceFeed:
    def __init__(self, engine: TickEngine) -> None:
        self._engine = engine
        self._task: asyncio.Task | None = None
        self.last_run_at: datetime | None = None
        self.last_updated_count: int = 0

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
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            try:
                self.last_updated_count = await self.refresh_active_instruments()
                self.last_run_at = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Real price feed tick failed")

    async def refresh_active_instruments(self) -> int:
        active_ids = [iid for iid, count in self._engine._subscriber_counts.items() if count > 0]
        if not active_ids:
            return 0
        market_open = nse_market_open(datetime.now(timezone.utc))
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Instrument).where(Instrument.id.in_(active_ids)))
            instruments = result.scalars().all()

        updated = 0
        for instrument in instruments:
            try:
                real = await fetch_real_price(instrument, market_open=market_open)
            except MarketDataSourceError:
                continue
            except Exception:
                logger.exception("Real price refresh failed for %s", instrument.symbol)
                continue
            if real is not None:
                price, source = real
                self._engine.set_real_price(instrument.id, price, source)
                updated += 1
        return updated

    async def refresh_instrument_now(self, instrument_id: uuid.UUID) -> None:
        """Fire-and-forget: called right after a subscribe so the UI does
        not have to wait up to REFRESH_INTERVAL_SECONDS for its first
        real price."""
        async with AsyncSessionLocal() as db:
            instrument = await db.get(Instrument, instrument_id)
        if instrument is None:
            return
        try:
            real = await fetch_real_price(instrument)
        except MarketDataSourceError:
            return
        except Exception:
            logger.exception("Immediate real price refresh failed for %s", instrument.symbol)
            return
        if real is not None:
            price, source = real
            self._engine.set_real_price(instrument.id, price, source)


real_price_feed = RealPriceFeed(tick_engine)
