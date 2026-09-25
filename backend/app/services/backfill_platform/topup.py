"""The daily automatic top-up, and "Top up now".

Every NSE trading day at the configured time (16:15 IST by default --
after the 15:30 close), for each Zerodha segment switched on: one job per
symbol and timeframe that is behind the last closed session, fetching from
that pair's last saved day (bf_coverage) -- not the source's whole default
history. It only tops up what is already tracked: pairs with no saved bars
(e.g. option strikes Kite never returned data for) and expired contracts
are left alone.

A run needs a Zerodha login: without one it waits ("waiting_login") and
starts as soon as there is one. A missed day needs no special handling --
the next run fetches everything after each pair's watermark.
"""

import asyncio
import logging
from datetime import datetime, time, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import (
    JOB_PRIORITY_BULK,
    JOB_PRIORITY_SCHEDULED,
    TOPUP_TIMEFRAME_ORDER,
    BfBackfillJob,
    BfBackfillRun,
    BfBackfillStatus,
    BfCoverage,
    BfSymbol,
)
from app.services.backfill_platform.coverage import (
    IST,
    KITE_SOURCES,
    build_coverage,
    get_settings,
    ist_date,
    last_completed_session,
    sessions_behind,
)
from app.services.backfill_platform.timeframes import timeframes_for_source
from app.services.backfill_platform.worker import backfill_worker
from app.services.broker.kite_ticker_service import find_connected_zerodha_account

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
RUN_WAITING_LOGIN = "waiting_login"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_CANCELLED = "cancelled"
RUN_SKIPPED = "skipped"
_OPEN_JOB_STATUSES = (BfBackfillStatus.PENDING.value, BfBackfillStatus.RUNNING.value)


class ZerodhaNotConnected(Exception):
    pass


def _topup_time(value: str) -> time:
    try:
        hours, minutes = (int(part) for part in value.split(":"))
        return time(hours, minutes)
    except ValueError:
        return time(16, 15)


def enabled_sources(settings) -> list[str]:
    return [s for s, on in (("zerodha", settings.auto_topup_zerodha), ("zerodha_nfo", settings.auto_topup_zerodha_nfo)) if on]


async def queue_topup_jobs(
    db: AsyncSession, run: BfBackfillRun, timeframes: list[str], user_id, now: datetime, base_priority: int,
) -> int:
    """Adds a job for every pair of `run.source` behind the last session."""
    native = {o.value for o in timeframes_for_source(run.source) if o.native}
    wanted = [tf for tf in timeframes if tf in native]
    rows = (
        await db.execute(
            select(BfCoverage, BfSymbol)
            .join(BfSymbol, BfSymbol.id == BfCoverage.symbol_id)
            .where(BfSymbol.source == run.source, BfCoverage.timeframe.in_(wanted))
        )
    ).all()
    queued = 0
    for coverage, symbol in rows:
        last_day = ist_date(coverage.last_ts)
        if symbol.expiry is not None and symbol.expiry <= last_day:
            continue  # expired contract: nothing trades after its last saved day
        if sessions_behind(coverage.last_ts, coverage.timeframe, now, coverage.checked_through) == 0:
            continue
        db.add(BfBackfillJob(
            symbol_id=symbol.id, source=run.source, timeframe=coverage.timeframe,
            start_date=last_day, end_date=run.session_date, requested_by=user_id, run_id=run.id,
            priority=base_priority + TOPUP_TIMEFRAME_ORDER.index(coverage.timeframe),
        ))
        queued += 1
    run.jobs_total = queued
    run.status = RUN_RUNNING if queued else RUN_COMPLETED
    run.started_at = now
    if not queued:
        run.completed_at = now
        run.message = "Already up to date"
    return queued


async def start_manual_topup(db: AsyncSession, requested_by, sources: list[str] | None = None) -> list[BfBackfillRun]:
    """"Top up now": queues the same jobs as the daily run, straight away,
    ahead of any scheduled work. Raises ZerodhaNotConnected without a login."""
    account = await find_connected_zerodha_account(db)
    if account is None:
        raise ZerodhaNotConnected("Log in to Zerodha first (Settings > Brokers).")
    settings = await get_settings(db)
    now = datetime.now(timezone.utc)
    runs = []
    for source in sources or list(KITE_SOURCES):
        run = BfBackfillRun(kind="manual", source=source, session_date=last_completed_session(now), status=RUN_RUNNING, requested_by=requested_by)
        db.add(run)
        await db.flush()
        await queue_topup_jobs(db, run, settings.topup_timeframes, account.user_id, now, JOB_PRIORITY_BULK)
        runs.append(run)
    await db.commit()
    backfill_worker.wake()
    return runs


async def cancel_run(db: AsyncSession, run: BfBackfillRun) -> int:
    """Stops a run: its queued jobs are cancelled (a job already running finishes)."""
    result = await db.execute(
        update(BfBackfillJob)
        .where(BfBackfillJob.run_id == run.id, BfBackfillJob.status == BfBackfillStatus.PENDING.value)
        .values(status=BfBackfillStatus.CANCELLED.value, completed_at=datetime.now(timezone.utc), error_message="Cancelled")
    )
    run.status = RUN_CANCELLED
    run.completed_at = datetime.now(timezone.utc)
    await db.commit()
    return result.rowcount


async def run_counts(db: AsyncSession, run_id) -> dict[str, int]:
    rows = (
        await db.execute(select(BfBackfillJob.status, func.count()).where(BfBackfillJob.run_id == run_id).group_by(BfBackfillJob.status))
    ).all()
    return {status: count for status, count in rows}


class BackfillTopupScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_check_at: datetime | None = None
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
        try:
            await build_coverage()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Building backfill coverage failed")
            self.last_error = f"Building coverage failed: {exc}"
        while True:
            try:
                await self.tick(datetime.now(timezone.utc))
                self.last_check_at = datetime.now(timezone.utc)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Backfill top-up tick failed")
                self.last_error = str(exc)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def tick(self, now: datetime) -> None:
        async with AsyncSessionLocal() as db:
            await self._finish_runs(db, now)
            settings = await get_settings(db)
            if settings.coverage_built_at is None:
                return
            session = last_completed_session(now)
            if now < datetime.combine(session, _topup_time(settings.topup_time), tzinfo=IST):
                return
            # A run still waiting for a login from an earlier session is
            # superseded: the new one fetches everything after each watermark.
            await db.execute(
                update(BfBackfillRun)
                .where(BfBackfillRun.status == RUN_WAITING_LOGIN, BfBackfillRun.session_date < session)
                .values(status=RUN_SKIPPED, completed_at=now, message="Covered by the next top-up")
            )
            account = None
            for source in enabled_sources(settings):
                run = (
                    await db.execute(
                        select(BfBackfillRun).where(
                            BfBackfillRun.kind == "scheduled", BfBackfillRun.source == source, BfBackfillRun.session_date == session,
                        )
                    )
                ).scalars().first()
                if run is not None and run.status != RUN_WAITING_LOGIN:
                    continue
                account = account or await find_connected_zerodha_account(db)
                if run is None:
                    run = BfBackfillRun(kind="scheduled", source=source, session_date=session, status=RUN_WAITING_LOGIN)
                    db.add(run)
                    await db.flush()
                if account is None:
                    run.message = "Waiting for Zerodha login"
                    continue
                run.message = None
                await queue_topup_jobs(db, run, settings.topup_timeframes, account.user_id, now, JOB_PRIORITY_SCHEDULED)
            await db.commit()
        backfill_worker.wake()

    async def _finish_runs(self, db: AsyncSession, now: datetime) -> None:
        """Marks a running run complete once none of its jobs are left."""
        for run in (await db.execute(select(BfBackfillRun).where(BfBackfillRun.status == RUN_RUNNING))).scalars().all():
            counts = await run_counts(db, run.id)
            if any(counts.get(s) for s in _OPEN_JOB_STATUSES):
                continue
            failed = counts.get(BfBackfillStatus.FAILED.value, 0)
            done = counts.get(BfBackfillStatus.COMPLETED.value, 0)
            run.status = RUN_COMPLETED
            run.completed_at = now
            run.message = f"{done:,} done, {failed:,} failed" if failed else f"{done:,} done"
        await db.commit()


backfill_topup_scheduler = BackfillTopupScheduler()
