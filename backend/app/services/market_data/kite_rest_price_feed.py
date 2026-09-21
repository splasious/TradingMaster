"""REST-polling fallback price feed for NSE equities, feeding `TickEngine`
the same way `real_price_feed.py` does for Delta.

Why this exists: unlike Delta (a public REST ticker RealPriceFeed already
polls), NSE equity live prices normally come exclusively from
`kite_ticker_service.py`'s WebSocket push stream. That stream has no REST
fallback of its own -- if the WebSocket handshake is failing (confirmed
live: Kite's ticker endpoint returning 403 Forbidden continuously, with
REST calls using the SAME credentials succeeding fine) every NSE
instrument's "real" price in TickEngine just stops updating entirely,
silently, for as long as the WebSocket stays down. Paper trading's mark-
to-market (TickEngine.get_current_price) and the Markets page both read
straight from TickEngine, so a stuck price there means a stuck "Live
Value"/P&L with no visible error anywhere -- exactly the symptom this
was built to fix.

Kept as its own polling loop rather than folded into RealPriceFeed's
fetch_real_price(): Kite's /quote/ltp needs an authenticated session
(api_key + access_token from a connected BrokerAccount, resolved once per
cycle, not per instrument) and is genuinely a batch endpoint (many
instruments per HTTP call) -- a different shape from RealPriceFeed's
"one public, unauthenticated call per instrument" abstraction, which
this deliberately doesn't disturb.

Runs unconditionally (not just when kite_ticker looks unhealthy) --
whenever the WebSocket ticker IS working, its pushes are far more
frequent than this feed's REFRESH_INTERVAL_SECONDS poll, so they simply
keep overwriting TickEngine's value between this feed's polls; no
failover branching needed. When the WebSocket is down, this is what
keeps prices moving at all.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker
from app.services.market_data.hours import nse_market_open
from app.services.market_data.tick_engine import TickEngine, tick_engine

logger = logging.getLogger(__name__)
REFRESH_INTERVAL_SECONDS = 15
# Kite's /quote/ltp documents a per-request instrument cap (500); chunked
# well under that so one slow/odd batch doesn't risk the whole cycle.
BATCH_SIZE = 200


class KiteRestPriceFeed:
    def __init__(self, engine: TickEngine) -> None:
        self._engine = engine
        self._task: asyncio.Task | None = None
        self.last_run_at: datetime | None = None
        self.last_updated_count: int = 0
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
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            try:
                self.last_updated_count = await self.refresh_active_instruments()
                self.last_run_at = datetime.now(timezone.utc)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Kite REST price feed tick failed")
                self.last_error = f"{type(exc).__name__}: {exc}"

    async def refresh_active_instruments(self, now: datetime | None = None) -> int:
        if not nse_market_open(now or datetime.now(timezone.utc)):
            return 0
        active_ids = [iid for iid, count in self._engine._subscriber_counts.items() if count > 0]
        if not active_ids:
            return 0

        async with AsyncSessionLocal() as db:
            creds = await find_connected_zerodha_credentials(db)
            if creds is None:
                return 0  # no connected Zerodha account -- nothing to poll with
            result = await db.execute(
                select(Instrument).where(Instrument.id.in_(active_ids), Instrument.data_source == "zerodha_kite")
            )
            instruments = result.scalars().all()

        if not instruments:
            return 0

        broker = ZerodhaKiteBroker()
        broker._api_key = creds["api_key"]
        broker._access_token = creds["access_token"]

        by_instrument_key = {f"{i.exchange}:{i.external_ref}": i for i in instruments}
        keys = list(by_instrument_key.keys())
        batches = [keys[i : i + BATCH_SIZE] for i in range(0, len(keys), BATCH_SIZE)]

        updated = 0
        for batch in batches:
            try:
                prices = await broker.get_ltp_batch(batch)
            except KiteAPIError:
                logger.exception("Kite REST LTP batch fetch failed (%d instruments)", len(batch))
                continue
            for key, price in prices.items():
                instrument = by_instrument_key.get(key)
                if instrument is None:
                    continue
                self._engine.set_real_price(instrument.id, price, source="kite_rest")
                updated += 1
        return updated


kite_rest_price_feed = KiteRestPriceFeed(tick_engine)
