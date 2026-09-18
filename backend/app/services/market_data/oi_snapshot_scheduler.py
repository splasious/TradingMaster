"""Snapshots live Kite ticker open interest (and price) from `TickEngine`
into 15-minute `OhlcvCandle` bars for every tracked NFO option contract, so
`services/options/pcr.py` reads data that's at most one snapshot cycle
stale instead of whatever a one-off backfill captured and then never
revisited.

`kite_ticker_service.py` already subscribes to every NFO option
`Instrument` row in the catalog (MODE_FULL, which is the mode that carries
OI) and keeps `TickEngine`'s in-memory `_real_oi`/`_real_price` current for
each of them on every tick. `nfo_expiry_rotation_scheduler.py` in turn
keeps that catalog itself covering exactly the nearest `EXPIRIES_TO_MAINTAIN`
(4) weekly expiries -- matching `compute_effective_pcr`'s own
`num_expiries=4` default. The one missing link was persistence: nothing
ever wrote that live in-memory OI back into `ohlcv_candles`, the table
`compute_pcr_series` actually reads -- so PCR silently kept reading
whatever a contract's OI was the moment it was first backfilled into the
catalog, potentially days or weeks stale, with no further updates ever
happening for a contract already present. This closes that gap the same
way `active_timeframe_sync_scheduler.py` does for regular deployment
instruments, just sourced from live ticks instead of a REST poll, and
scoped to the whole tracked NFO option catalog instead of active-deployment
pairs (a native/"Advanced Python" strategy has no fixed `instrument_id` to
scope a sync off of, so this can't reuse that scheduler's pair-selection
logic).

Upserts the CURRENT 15-minute bucket in place every cycle rather than
insert-once-per-bucket-and-forget: OI is a point-in-time snapshot like
close, not additive (see resample.py's own comment on this), so each pass
just overwrites it with whatever TickEngine has most recently. By the time
a bucket's 15 minutes are up, its stored value is whatever was true right
before it closed -- which is what a strategy or the dashboard reading
"the latest bar" should see, not just the first tick that happened to land
in that window.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.kite_ticker_service import kite_ticker_service
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine

logger = logging.getLogger(__name__)

# Independent of kite_ticker_service's own 300s catalog-refresh cadence --
# this only reads TickEngine's already-current in-memory state, so it can
# run far more often without hitting Kite's API at all. Matches
# active_timeframe_sync_scheduler's SYNC_INTERVAL_SECONDS so both
# "keep candles current" jobs share one cadence.
SNAPSHOT_INTERVAL_SECONDS = 60
TIMEFRAME = "15m"
BUCKET_MINUTES = 15


def _bucket_start(now: datetime) -> datetime:
    floor_minute = (now.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    return now.replace(minute=floor_minute, second=0, microsecond=0)


async def _tracked_option_ids(db: AsyncSession) -> list[uuid.UUID]:
    """Every live NFO option contract PCR could plausibly read -- not
    hand-limited to 4 expiries here (that's compute_effective_pcr's own
    `.limit(num_expiries)`, applied at read time): nfo_expiry_rotation.py
    already keeps the catalog itself windowed to what matters, so this
    just tracks whatever it's put there and lets a lapsed contract fall
    out naturally once its expiry is in the past."""
    today_ist = datetime.now(timezone.utc).astimezone(IST).date()
    rows = (
        await db.execute(
            select(Instrument.id).where(
                Instrument.exchange == "NFO",
                Instrument.instrument_type == "option",
                Instrument.data_source == "zerodha_kite",
                Instrument.expiry.is_not(None),
                Instrument.expiry >= today_ist,
            )
        )
    ).scalars().all()
    return list(rows)


async def snapshot_once(db: AsyncSession) -> int:
    """One pass: for every tracked NFO option with a live OI on file right
    now, upsert the current 15m bucket. Returns how many contracts were
    written (0 when the ticker has nothing live yet, e.g. outside market
    hours or before the first tick of the day)."""
    instrument_ids = await _tracked_option_ids(db)
    if not instrument_ids:
        return 0

    live: dict[uuid.UUID, tuple[float, float | None]] = {}
    for instrument_id in instrument_ids:
        oi = tick_engine.get_current_oi(instrument_id)
        if oi is None:
            continue
        live[instrument_id] = (oi, tick_engine.get_current_price(instrument_id))
    if not live:
        return 0

    bucket_ts = _bucket_start(datetime.now(timezone.utc))
    existing_rows = (
        await db.execute(
            select(OhlcvCandle).where(
                OhlcvCandle.instrument_id.in_(live.keys()),
                OhlcvCandle.timeframe == TIMEFRAME,
                OhlcvCandle.ts == bucket_ts,
            )
        )
    ).scalars().all()
    existing_by_instrument = {row.instrument_id: row for row in existing_rows}

    written = 0
    for instrument_id, (oi, price) in live.items():
        row = existing_by_instrument.get(instrument_id)
        if row is not None:
            row.open_interest = oi
            if price is not None:
                row.close = price
                row.high = max(row.high, price)
                row.low = min(row.low, price)
            written += 1
        elif price is not None:
            # A brand-new bucket needs real OHLC, not just OI -- skip
            # (rather than fabricate open/high/low from nothing) on the
            # rare tick that has OI but no price yet; the next cycle
            # within the same still-open bucket almost always has both,
            # since Kite's MODE_FULL ticks carry them together.
            db.add(
                OhlcvCandle(
                    instrument_id=instrument_id, timeframe=TIMEFRAME, ts=bucket_ts,
                    open=price, high=price, low=price, close=price,
                    open_interest=oi, source="kite_live",
                )
            )
            written += 1
    await db.commit()
    return written


class OiSnapshotScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_run_at: datetime | None = None
        self.last_written_count: int = 0
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
            try:
                ticker = kite_ticker_service._ticker
                if ticker is None or not ticker.is_connected():
                    # Don't re-persist whatever's still sitting in TickEngine
                    # from before the WebSocket died -- that's exactly what
                    # silently masked a multi-hour-stale PCR as "just
                    # written" once before (the bucket timestamp looked
                    # fresh even though the OI value inside it hadn't
                    # actually changed in hours). Skipping here means a
                    # dead ticker shows up as 0 written / a clear error
                    # instead of quietly lying about freshness.
                    self.last_written_count = 0
                    self.last_error = "kite ticker not connected -- skipping to avoid re-persisting stale OI"
                else:
                    async with AsyncSessionLocal() as db:
                        self.last_written_count = await snapshot_once(db)
                    self.last_error = None
                self.last_run_at = datetime.now(timezone.utc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("OI snapshot cycle failed")
                self.last_error = "snapshot failed, see logs"
            await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)


oi_snapshot_scheduler = OiSnapshotScheduler()
