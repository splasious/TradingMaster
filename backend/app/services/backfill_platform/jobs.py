"""Runs one Data Backfill Platform job: fetches the requested range from
its source, saves the finished candles (skipping any already stored) and
updates the symbol's coverage row. Jobs are queued in the database and run
one at a time by BackfillWorker (worker.py)."""

import asyncio
import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfOhlcvBar, BfSymbol
from app.services.backfill_platform.coverage import (
    KITE_SOURCES,
    last_completed_session,
    mark_checked,
    nse_bar_complete,
    recompute_coverage,
)
from app.services.backfill_platform.kite_auth import get_authenticated_kite_broker
from app.services.broker.zerodha_broker import KiteAPIError
from app.services.market_data.bar_periods import is_complete
from app.services.market_data.base import Bar, MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource

logger = logging.getLogger(__name__)


# A job is tried at most this many times -- a failure that looks temporary
# (network, rate limit, the source's server) is retried after a back-off,
# and a restart mid-job counts as an attempt too.
MAX_ATTEMPTS = 3
_RETRY_BACKOFF = [timedelta(minutes=1), timedelta(minutes=5)]
# Failures a retry can't fix: a missing symbol or timeframe, and no usable
# Zerodha login (Kite's TokenException is an expired one) -- the scheduler
# waits for a login instead.
_PERMANENT_ERRORS = (
    "not found", "does not support", "no longer exists", "unknown source", "no zerodha", "no credentials",
    "not authenticated", "tokenexception", "inputexception", "permissionexception",
)


async def requeue_interrupted_jobs_on_startup() -> int:
    """Jobs wait in the database, so a restart loses nothing: one that was
    running when the process stopped goes back in the queue (queued ones
    are still there). It fails only after MAX_ATTEMPTS -- a job that keeps
    taking the process down must not loop forever. Called once at startup,
    before the worker starts."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BfBackfillJob).where(BfBackfillJob.status == BfBackfillStatus.RUNNING.value))
        interrupted = list(result.scalars().all())
        for job in interrupted:
            if job.attempts >= MAX_ATTEMPTS:
                job.status = BfBackfillStatus.FAILED.value
                job.error_message = f"{INTERRUPTED_PREFIX} {job.attempts} times -- stopped retrying."
                job.completed_at = datetime.now(timezone.utc)
            else:
                job.status = BfBackfillStatus.PENDING.value
        await db.commit()
        return len(interrupted)


INTERRUPTED_PREFIX = "Interrupted by a server restart"


def unresolved_failures(now: datetime, days: int = 7):
    """Conditions for failed jobs of the last `days` that nothing later has
    redone -- no newer job for the same symbol and timeframe that completed
    or is queued. What "Needs attention" counts and "Retry" re-queues."""
    later = aliased(BfBackfillJob)
    redone = exists().where(and_(
        later.symbol_id == BfBackfillJob.symbol_id, later.timeframe == BfBackfillJob.timeframe,
        later.status.in_([BfBackfillStatus.COMPLETED.value, BfBackfillStatus.PENDING.value, BfBackfillStatus.RUNNING.value]),
        later.created_at > BfBackfillJob.created_at,
    ))
    return (
        BfBackfillJob.status == BfBackfillStatus.FAILED.value,
        BfBackfillJob.completed_at >= now - timedelta(days=days),
        ~redone,
    )


async def requeue_failed(db: AsyncSession, kind: str, priority: int) -> int:
    """Puts unresolved failures back in the queue as fresh attempts --
    `kind` "interrupted" (stopped by a restart) or "failed" (the rest)."""
    now = datetime.now(timezone.utc)
    interrupted = BfBackfillJob.error_message.like(INTERRUPTED_PREFIX + "%")
    rows = (
        await db.execute(select(BfBackfillJob).where(*unresolved_failures(now), interrupted if kind == "interrupted" else ~interrupted))
    ).scalars().all()
    for job in rows:
        job.status = BfBackfillStatus.PENDING.value
        job.attempts = 0
        job.priority = priority
        job.run_after = job.started_at = job.completed_at = job.error_message = None
    await db.commit()
    return len(rows)


def _is_permanent(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in _PERMANENT_ERRORS)


def _fail_or_retry(job: BfBackfillJob, error: str) -> None:
    """Puts the job back in the queue after a back-off, or fails it for good."""
    now = datetime.now(timezone.utc)
    if job.attempts < MAX_ATTEMPTS and not _is_permanent(error):
        job.status = BfBackfillStatus.PENDING.value
        job.run_after = now + _RETRY_BACKOFF[min(job.attempts, len(_RETRY_BACKOFF)) - 1]
        job.error_message = f"Attempt {job.attempts} of {MAX_ATTEMPTS} failed, retrying: {error}"[:_ERROR_MESSAGE_MAX]
    else:
        job.status = BfBackfillStatus.FAILED.value
        job.error_message = error[:_ERROR_MESSAGE_MAX]
        job.completed_at = now


def _checked_session(end_date: date | None, now: datetime) -> date:
    """The last session a successful fetch covered: its end date, or the
    last closed session when it ran up to now."""
    latest = last_completed_session(now)
    return min(end_date, latest) if end_date is not None else latest


def _finished(source: str, timeframe: str, bars: list[Bar], now: datetime) -> list[Bar]:
    """Drops the candle still forming: saved now it would keep its partial
    values for good, since a stored bar is never overwritten. A Kite
    candle is final at the NSE close at the latest (coverage.nse_bar_end)."""
    if source in KITE_SOURCES:
        return [bar for bar in bars if nse_bar_complete(bar["ts"], timeframe, now)]
    return [bar for bar in bars if is_complete(bar["ts"], timeframe, now)]


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
        # Kite's candles come IST-stamped -- stored as UTC, like everything
        # else, so SQLite (which drops the offset) reads back the same instant.
        unique.setdefault(as_aware_utc(bar["ts"]).astimezone(timezone.utc), bar)
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
    """Never raises -- the worker runs job after job, and one job's
    exception must not stop the queue or leave the job "running". Any
    failure is retried (see _fail_or_retry) or ends the job as "failed"
    with the reason, keeping whatever bars were fetched before it."""
    try:
        await _run_job(job_id)
    except Exception as exc:
        logger.exception("Backfill job %s failed", job_id)
        async with AsyncSessionLocal() as db:
            job = await db.get(BfBackfillJob, job_id)
            if job is not None and job.status == BfBackfillStatus.RUNNING.value:
                _fail_or_retry(job, f"{type(exc).__name__}: {exc}")
                await db.commit()


async def _run_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(BfBackfillJob, job_id)
        if job is None or job.status != BfBackfillStatus.PENDING.value:
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
        job.attempts += 1
        job.run_after = None
        await db.commit()

        bars: list[Bar] = []
        fetch_error: str | None = None
        windows = _windows(job.source, job.timeframe, _to_datetime(job.start_date), _to_datetime(job.end_date, end_of_day=True))
        for n, (start, end) in enumerate(windows):
            if n and job.source in KITE_SOURCES:
                await asyncio.sleep(_KITE_PACING_SECONDS)
            try:
                bars += await _fetch_bars(db, job.source, symbol_row.symbol, job.timeframe, start, end, job.requested_by)
            except MarketDataSourceError as exc:
                fetch_error = str(exc)
                if len(windows) > 1:
                    fetch_error = f"Part {n + 1} of {len(windows)} ({start:%d %b %Y} to {end:%d %b %Y}) failed: {exc}"
                break

        now = datetime.now(timezone.utc)
        bars = _finished(job.source, job.timeframe, bars, now)
        downloaded, inserted = await save_bars(db, symbol_row.id, job.timeframe, bars)
        if inserted:
            await recompute_coverage(db, symbol_row.id, job.timeframe)
        if fetch_error is None and job.source in KITE_SOURCES:
            await mark_checked(db, symbol_row.id, job.timeframe, _checked_session(job.end_date, now))
        job.downloaded_count = downloaded
        job.inserted_count = inserted
        job.duplicate_count = downloaded - inserted
        if fetch_error is None:
            job.status = BfBackfillStatus.COMPLETED.value
            job.error_message = None
            job.completed_at = datetime.now(timezone.utc)
        else:
            prefix = f"Saved {inserted} new bars, then: " if bars else ""
            _fail_or_retry(job, prefix + fetch_error)
        await db.commit()
