"""Keeps `ohlcv_candles` fresh for every (instrument, timeframe) pair an
active paper or live deployment actually trades on -- not just the
1-minute timeframe BfLiveSyncScheduler maintains for the Data Backfill
Platform's own isolated bf_* schema. A strategy running on, say, 15m
otherwise only ever sees whatever was last manually backfilled: the
indicator history goes stale the moment nothing re-runs that backfill,
which starves the strategy's own signal of fresh data and leaves the
Charts page showing frozen candles past that point.

Deliberately scoped to only the (instrument, timeframe) pairs genuinely
in use, not every symbol/timeframe combination the app knows about --
Delta's own catalog alone is 200+ symbols; polling all of them across
every timeframe on a fixed interval would be pure waste for the vast
majority never actually deployed against.

Zerodha-sourced instruments (any NIFTY/BANKNIFTY index, option, or
future -- `data_source == "zerodha_kite"`) go through a separate branch
below rather than the generic `registry.py` lookup: unlike Delta's public,
stateless REST calls, Kite's historical API needs a specific connected
account's session and a `segment` ("NFO" vs "NSE") the generic
`MarketDataSource` interface has no room for. This resolves the one
connected account via `find_connected_zerodha_credentials`
(kite_ticker_service.py) -- the same pattern `nfo_expiry_rotation.py`
already established -- once per tick, not once per pair. Deliberately a
narrower, safer scope than `live_sync_scheduler.py`'s existing Zerodha
exclusion: that scheduler polls its *entire* tracked symbol universe
unattended (which its own docstring explicitly declines to do for
Zerodha); this one only ever touches the (instrument, timeframe) pairs a
currently-ACTIVE deployment genuinely trades on, so the same caution
doesn't carry over.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperDeployment
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.registry import get_market_data_source

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 60
LOOKBACK = timedelta(days=2)


class ActiveTimeframeSyncScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_sync_at: datetime | None = None
        self.last_synced_count: int = 0
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
                self.last_synced_count = await self.sync_once()
                self.last_sync_at = datetime.now(timezone.utc)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Active timeframe sync tick failed")
                self.last_error = str(exc)

    async def sync_once(self) -> int:
        async with AsyncSessionLocal() as db:
            return await self.sync(db)

    async def sync(self, db: AsyncSession) -> int:
        """The syncable core, taking an explicit session -- split out from
        sync_once() so tests can exercise it against their own isolated
        session instead of the module-level AsyncSessionLocal."""
        paper_pairs = (
            await db.execute(
                select(PaperDeployment.instrument_id, PaperDeployment.timeframe)
                .where(PaperDeployment.status == DeploymentStatus.ACTIVE.value)
                .distinct()
            )
        ).all()
        live_pairs = (
            await db.execute(
                select(LiveDeployment.instrument_id, LiveDeployment.timeframe)
                .where(LiveDeployment.status == "active")
                .distinct()
            )
        ).all()
        pairs = {(instrument_id, timeframe) for instrument_id, timeframe in [*paper_pairs, *live_pairs]}
        if not pairs:
            return 0

        instrument_ids = {instrument_id for instrument_id, _ in pairs}
        instruments = {
            i.id: i for i in (await db.execute(select(Instrument).where(Instrument.id.in_(instrument_ids)))).scalars()
        }

        # Resolved once per tick, not once per pair -- same construction
        # pattern nfo_expiry_rotation.py/history_depth.py already use
        # (direct _api_key/_access_token, no extra /user/profile
        # round-trip; kite_session_monitor_scheduler already validates
        # session health independently every 15 min).
        zerodha_creds = await find_connected_zerodha_credentials(db)
        zerodha_broker: ZerodhaKiteBroker | None = None
        if zerodha_creds is not None:
            zerodha_broker = ZerodhaKiteBroker()
            zerodha_broker._api_key = zerodha_creds["api_key"]
            zerodha_broker._access_token = zerodha_creds["access_token"]

        now = datetime.now(timezone.utc)
        synced = 0
        skipped_zerodha = 0
        for instrument_id, timeframe in pairs:
            instrument = instruments.get(instrument_id)
            if instrument is None:
                continue

            try:
                if instrument.data_source == "zerodha_kite":
                    if zerodha_broker is None:
                        skipped_zerodha += 1
                        continue
                    # Segment ("NFO" vs "NSE") comes straight from the
                    # instrument's own exchange -- correctly covers the
                    # index itself (NSE) and any NFO option/future alike.
                    bars = await zerodha_broker.get_historical_data(
                        instrument.external_ref, timeframe, now - LOOKBACK, now, instrument.exchange
                    )
                else:
                    source = get_market_data_source(instrument.data_source)
                    bars = await source.get_historical_data(instrument.external_ref, timeframe, now - LOOKBACK, now)
            except (MarketDataSourceError, KiteAPIError):
                continue
            except Exception:
                logger.exception("Active timeframe sync failed for %s:%s", instrument.symbol, timeframe)
                continue
            if not bars:
                continue

            existing_result = await db.execute(
                select(OhlcvCandle.ts).where(
                    OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == timeframe
                )
            )
            existing_ts = {as_aware_utc(ts) for ts in existing_result.scalars().all()}
            for bar in bars:
                bar_ts = as_aware_utc(bar["ts"])
                if bar_ts in existing_ts:
                    continue
                db.add(
                    OhlcvCandle(
                        instrument_id=instrument.id, timeframe=timeframe, ts=bar_ts,
                        open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"],
                        volume=bar.get("volume"), open_interest=bar.get("open_interest"),
                        source=instrument.data_source,
                    )
                )
                existing_ts.add(bar_ts)
            await db.commit()
            synced += 1

        if skipped_zerodha:
            logger.info(
                "Skipped %d Zerodha-sourced pair(s) this tick -- no connected Zerodha account", skipped_zerodha
            )
        return synced


active_timeframe_sync_scheduler = ActiveTimeframeSyncScheduler()
