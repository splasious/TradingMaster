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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperDeployment
from app.services.backfill_platform.timeframes import DERIVABLE_FROM_DAILY
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.registry import get_market_data_source

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 60
LOOKBACK = timedelta(days=2)

# Kite has no native weekly/monthly interval at all (KITE_INTERVAL_MAP has
# no "1wk"/"1mo" entry) -- a "1wk"/"1mo" Zerodha pair fetches/stores "1d"
# instead; load_candles' resample-on-read (paper/live trading engines,
# Charts) derives the real weekly/monthly bars from that at read time.
_DAILY_BACKFILL_LOOKBACK = timedelta(days=400)
# Below this many stored daily bars, a derived-timeframe pair hasn't got
# enough underlying history to produce a real weekly/monthly bar yet (60
# weekly bars, the trading engines' own LOOKBACK_BARS, needs roughly this
# many trading days) -- pull the wide one-time window instead of the
# normal 2-day incremental one until it does.
_MIN_DAILY_BARS_FOR_DERIVED_TIMEFRAME = 90


async def _active_pairs_with_instruments(db: AsyncSession) -> tuple[set[tuple], dict]:
    """Every (instrument_id, timeframe) an ACTIVE paper or live deployment
    currently uses, plus the Instrument rows themselves -- shared between
    sync() and diagnose_active_pairs() so there's one query, not two
    near-duplicates."""
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
        return pairs, {}

    instrument_ids = {instrument_id for instrument_id, _ in pairs}
    instruments = {
        i.id: i for i in (await db.execute(select(Instrument).where(Instrument.id.in_(instrument_ids)))).scalars()
    }
    return pairs, instruments


async def diagnose_active_pairs(db: AsyncSession) -> list[dict]:
    """For every (instrument, timeframe) an active deployment is actually
    using: the symbol, data source, and the most recent stored candle's
    timestamp for that exact pair -- read-only, no live broker calls.
    Exists so "why isn't my chart showing today's candle" is answerable
    directly (is this pair even one the scheduler is supposed to be
    covering, and how stale is what's actually stored) rather than
    guessing from outside."""
    pairs, instruments = await _active_pairs_with_instruments(db)
    rows: list[dict] = []
    for instrument_id, timeframe in pairs:
        instrument = instruments.get(instrument_id)
        if instrument is None:
            continue
        # A Zerodha "1wk"/"1mo" pair is actually stored under "1d" (see
        # sync()'s own comment) -- check that, or this would always show
        # null even once the pair is genuinely covered via resample-on-read.
        stored_timeframe = "1d" if instrument.data_source == "zerodha_kite" and timeframe in DERIVABLE_FROM_DAILY else timeframe
        latest_ts = (
            await db.execute(
                select(OhlcvCandle.ts)
                .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == stored_timeframe)
                .order_by(OhlcvCandle.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        rows.append({
            "symbol": instrument.symbol, "external_ref": instrument.external_ref, "exchange": instrument.exchange,
            "timeframe": timeframe, "stored_as_timeframe": stored_timeframe, "data_source": instrument.data_source,
            "latest_candle_ts": as_aware_utc(latest_ts).isoformat() if latest_ts else None,
        })
    return rows


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
        pairs, instruments = await _active_pairs_with_instruments(db)
        if not pairs:
            return 0

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
        seen_zerodha_fetches: set[tuple] = set()
        for instrument_id, timeframe in pairs:
            instrument = instruments.get(instrument_id)
            if instrument is None:
                continue

            is_zerodha = instrument.data_source == "zerodha_kite"
            # Kite has no native weekly/monthly interval at all -- fetch
            # and store "1d" instead; load_candles' resample-on-read
            # (paper/live trading engines, Charts) derives the actual
            # 1wk/1mo bars from that at read time.
            fetch_timeframe = "1d" if is_zerodha and timeframe in DERIVABLE_FROM_DAILY else timeframe

            try:
                if is_zerodha:
                    if zerodha_broker is None:
                        skipped_zerodha += 1
                        continue
                    fetch_key = (instrument.id, fetch_timeframe)
                    if fetch_key in seen_zerodha_fetches:
                        # Both a "1wk" and a "1mo" pair on the same
                        # instrument both map to "1d" -- already fetched
                        # and stored it once this tick.
                        continue
                    seen_zerodha_fetches.add(fetch_key)

                    lookback = LOOKBACK
                    if fetch_timeframe != timeframe:
                        # A derived pair needs real underlying daily
                        # history before load_candles can produce a
                        # meaningful weekly/monthly bar -- pull the wide,
                        # one-time window until there's enough, then fall
                        # back to the normal incremental one.
                        existing_daily_count = (
                            await db.execute(
                                select(func.count()).select_from(OhlcvCandle).where(
                                    OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == "1d"
                                )
                            )
                        ).scalar_one()
                        if existing_daily_count < _MIN_DAILY_BARS_FOR_DERIVED_TIMEFRAME:
                            lookback = _DAILY_BACKFILL_LOOKBACK

                    # Segment ("NFO" vs "NSE") comes straight from the
                    # instrument's own exchange -- correctly covers the
                    # index itself (NSE) and any NFO option/future alike.
                    bars = await zerodha_broker.get_historical_data(
                        instrument.external_ref, fetch_timeframe, now - lookback, now, instrument.exchange
                    )
                else:
                    source = get_market_data_source(instrument.data_source)
                    bars = await source.get_historical_data(instrument.external_ref, fetch_timeframe, now - LOOKBACK, now)
            except (MarketDataSourceError, KiteAPIError):
                continue
            except Exception:
                logger.exception("Active timeframe sync failed for %s:%s", instrument.symbol, fetch_timeframe)
                continue
            if not bars:
                continue

            existing_result = await db.execute(
                select(OhlcvCandle.ts).where(
                    OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == fetch_timeframe
                )
            )
            existing_ts = {as_aware_utc(ts) for ts in existing_result.scalars().all()}
            for bar in bars:
                bar_ts = as_aware_utc(bar["ts"])
                if bar_ts in existing_ts:
                    continue
                db.add(
                    OhlcvCandle(
                        instrument_id=instrument.id, timeframe=fetch_timeframe, ts=bar_ts,
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
