"""Runs a Data Backfill Platform job in the background, mirroring
services/market_data/backfill.py's pattern (own DB session, real
source calls, real dedup) but source-scoped rather than instrument-catalog-
scoped, and honoring the requested date range (PRD 4.1's From/To pickers --
the main platform's backfill never exposed this)."""

import asyncio
import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfOhlcvBar, BfSymbol
from app.services.backfill_platform.kite_auth import get_authenticated_kite_broker
from app.services.broker.zerodha_broker import KiteAPIError
from app.services.market_data.base import Bar, MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource

logger = logging.getLogger(__name__)


async def fail_orphaned_jobs_on_startup() -> int:
    """Backfill jobs run as in-process BackgroundTasks, so a "pending" or
    "running" row can only mean the process that was supposed to run it is
    gone (a restart, a crash) -- nothing resumes them, and they'd otherwise
    sit forever looking like a stuck backfill. Called once from the
    lifespan startup hook, before this process could possibly have queued
    any job of its own, so every row found here is unambiguously orphaned."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BfBackfillJob).where(BfBackfillJob.status.in_([BfBackfillStatus.PENDING.value, BfBackfillStatus.RUNNING.value]))
        )
        orphaned = list(result.scalars().all())
        for job in orphaned:
            job.status = BfBackfillStatus.FAILED.value
            job.error_message = "Interrupted by a server restart -- re-run this backfill if still needed."
            job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return len(orphaned)


def _to_datetime(d: date | None, end_of_day: bool = False) -> datetime | None:
    if d is None:
        return None
    t = time(23, 59, 59) if end_of_day else time(0, 0, 0)
    return datetime.combine(d, t, tzinfo=timezone.utc)


# Kite Connect rejects a historical request spanning more than this many
# days for the interval ("interval exceeds max limit") -- its documented
# limits, less a day of margin. A longer backfill is fetched window by window.
_KITE_WINDOW_DAYS = {"1m": 59, "5m": 99, "15m": 199, "30m": 199, "60m": 399, "1d": 1999}
_KITE_PACING_SECONDS = 0.35  # Kite allows 3 historical requests a second
# Delta returns at most this many candles per request.
_DELTA_MAX_CANDLES = 2000
_DELTA_BAR_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "4h": 240, "1d": 1440, "1wk": 10080}
_INSERT_CHUNK = 1000  # rows per INSERT -- well under Postgres' 32,767 bind-parameter cap
_ERROR_MESSAGE_MAX = 1000  # BfBackfillJob.error_message's column length


def _windows(source: str, timeframe: str, start: datetime | None, end: datetime | None) -> list[tuple[datetime | None, datetime | None]]:
    """The requested range split into windows each source accepts in one
    request, oldest first. An open range (no start date) stays one request,
    as before -- each source then applies its own default lookback."""
    if start is None:
        return [(start, end)]
    end = end or datetime.now(timezone.utc)
    if source in ("zerodha", "zerodha_nfo"):
        step = timedelta(days=_KITE_WINDOW_DAYS.get(timeframe, 59))
    elif source == "delta":
        step = timedelta(minutes=_DELTA_BAR_MINUTES.get(timeframe, 1440) * _DELTA_MAX_CANDLES)
    else:
        return [(start, end)]
    windows = []
    while start < end:
        windows.append((start, min(start + step, end)))
        start = start + step
    return windows or [(start, end)]


async def _fetch_bars(db: AsyncSession, source: str, symbol: str, timeframe: str, start: datetime | None, end: datetime | None, user_id) -> list[Bar]:
    if source == "delta":
        return await DeltaExchangeDataSource().get_historical_data(symbol, timeframe, start, end)
    if source in ("zerodha", "zerodha_nfo"):
        segment = "NFO" if source == "zerodha_nfo" else "NSE"
        try:
            broker = await get_authenticated_kite_broker(db, user_id)
            return await broker.get_historical_data(symbol, timeframe, start, end, segment)  # type: ignore[return-value]
        except KiteAPIError as exc:
            raise MarketDataSourceError(str(exc)) from exc
    raise MarketDataSourceError(f"Unknown source '{source}'")


async def save_bars(db: AsyncSession, symbol_id: uuid.UUID, timeframe: str, bars: list[Bar]) -> tuple[int, int]:
    """Inserts the bars, skipping any already stored -- ON CONFLICT DO
    NOTHING, so two backfills of the same symbol running at once can't
    fail each other on the unique (symbol, timeframe, ts) constraint the way
    select-then-insert did. Returns (distinct bars, how many were new);
    adjoining fetch windows can both return the bar on their boundary."""
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    unique: dict[datetime, Bar] = {}
    for bar in bars:
        unique.setdefault(as_aware_utc(bar["ts"]), bar)
    rows = [
        {
            "id": uuid.uuid4(), "symbol_id": symbol_id, "timeframe": timeframe, "ts": ts,
            "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
            "volume": bar.get("volume"), "open_interest": bar.get("open_interest"),
        }
        for ts, bar in unique.items()
    ]
    inserted = 0
    for i in range(0, len(rows), _INSERT_CHUNK):
        result = await db.execute(
            insert(BfOhlcvBar).values(rows[i : i + _INSERT_CHUNK]).on_conflict_do_nothing(index_elements=["symbol_id", "timeframe", "ts"])
        )
        inserted += result.rowcount
    return len(rows), inserted


async def run_bf_backfill_job(job_id: uuid.UUID) -> None:
    """Never raises: "Backfill All" runs its jobs one after another in the
    same background task, so one job's exception used to stop every job
    queued behind it (left "pending") and leave itself "running" forever.
    Any failure now ends the job as "failed" with the reason, keeping
    whatever bars were fetched before it."""
    try:
        await _run_job(job_id)
    except Exception as exc:
        logger.exception("Backfill job %s failed", job_id)
        async with AsyncSessionLocal() as db:
            job = await db.get(BfBackfillJob, job_id)
            if job is not None and job.status not in (BfBackfillStatus.COMPLETED.value, BfBackfillStatus.FAILED.value):
                job.status = BfBackfillStatus.FAILED.value
                job.error_message = f"{type(exc).__name__}: {exc}"[:_ERROR_MESSAGE_MAX]
                job.completed_at = datetime.now(timezone.utc)
                await db.commit()


async def _run_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(BfBackfillJob, job_id)
        if job is None:
            return
        symbol_row = await db.get(BfSymbol, job.symbol_id)
        if symbol_row is None:
            job.status = BfBackfillStatus.FAILED.value
            job.error_message = "Symbol no longer exists"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        job.status = BfBackfillStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        bars: list[Bar] = []
        fetch_error: str | None = None
        windows = _windows(job.source, job.timeframe, _to_datetime(job.start_date), _to_datetime(job.end_date, end_of_day=True))
        for n, (start, end) in enumerate(windows):
            if n and job.source in ("zerodha", "zerodha_nfo"):
                await asyncio.sleep(_KITE_PACING_SECONDS)
            try:
                bars += await _fetch_bars(db, job.source, symbol_row.symbol, job.timeframe, start, end, job.requested_by)
            except MarketDataSourceError as exc:
                fetch_error = str(exc)
                if len(windows) > 1:
                    fetch_error = f"Part {n + 1} of {len(windows)} ({start:%d %b %Y} to {end:%d %b %Y}) failed: {exc}"
                break

        downloaded, inserted = await save_bars(db, symbol_row.id, job.timeframe, bars)
        job.downloaded_count = downloaded
        job.inserted_count = inserted
        job.duplicate_count = downloaded - inserted
        job.completed_at = datetime.now(timezone.utc)
        if fetch_error is None:
            job.status = BfBackfillStatus.COMPLETED.value
        else:
            job.status = BfBackfillStatus.FAILED.value
            prefix = f"Saved {inserted} new bars, then: " if bars else ""
            job.error_message = (prefix + fetch_error)[:_ERROR_MESSAGE_MAX]
        await db.commit()
