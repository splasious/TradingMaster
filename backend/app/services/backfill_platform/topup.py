"""The daily automatic top-up, and "Top up now".

Every NSE trading day at the configured time (16:15 IST by default --
after the 15:30 close), for each Zerodha segment switched on: one job per
symbol and timeframe that is behind the last closed session, fetching from
that pair's last saved day (bf_coverage) -- not the source's whole default
history. It tops up what is already tracked, plus -- for NFO -- every still-active
contract with no data yet (a strike that hasn't traded may start to), at
15m. Expired contracts are left alone: Kite serves no history for them.

A run needs a Zerodha login: without one it waits ("waiting_login") and
starts as soon as there is one. A missed day needs no special handling --
the next run fetches everything after each pair's watermark.
"""

import asyncio
import logging
from datetime import datetime, time, timezone

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.alert import Alert, AlertSeverity, AlertType
from app.models.backfill_platform import (
    JOB_PRIORITY_BULK,
    JOB_PRIORITY_SCHEDULED,
    BfBackfillJob,
    BfBackfillRun,
    BfBackfillStatus,
    BfCoverage,
    BfSymbol,
    topup_priority,
)
from app.services.backfill_platform.coverage import (
    IST,
    KITE_SOURCES,
    build_coverage,
    get_settings,
    is_trading_day,
    ist_date,
    last_completed_session,
    parse_hhmm,
    sessions_behind,
)
from app.models.user import Role, User, UserRole
from app.services.alerts.service import create_alert
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


# A contract with no saved bars is retried at the NFO rotation's timeframe.
EMPTY_CONTRACT_TIMEFRAME = "15m"
LOGIN_REMINDER_AT = time(8, 45)


class ZerodhaNotConnected(Exception):
    pass


def untraded_active_contracts(session):
    """NFO contracts with no saved bars that still trade on `session`."""
    return select(BfSymbol).where(
        BfSymbol.source == "zerodha_nfo",
        or_(BfSymbol.expiry.is_(None), BfSymbol.expiry >= session),
        ~exists().where(BfCoverage.symbol_id == BfSymbol.id),
    )


async def _admin_ids(db: AsyncSession) -> list:
    return list(
        (
            await db.execute(
                select(User.id).join(UserRole, UserRole.user_id == User.id).join(Role, Role.id == UserRole.role_id)
                .where(Role.name == "administrator").distinct()
            )
        ).scalars()
    )


async def _alert_admins(db: AsyncSession, *, key: str, severity: AlertSeverity, alert_type: str, title: str, message: str) -> None:
    """One in-app alert per administrator, once per `key`."""
    already = (await db.execute(select(Alert.id).where(Alert.object_type == "bf_backfill", Alert.object_id == key).limit(1))).first()
    if already:
        return
    for user_id in await _admin_ids(db):
        await create_alert(db, user_id=user_id, alert_type=alert_type, severity=severity, title=title, message=message,
                           object_type="bf_backfill", object_id=key)


def _topup_time(value: str) -> time:
    return parse_hhmm(value, time(16, 15))


def enabled_sources(settings) -> list[str]:
    return [s for s, on in (("zerodha", settings.auto_topup_zerodha), ("zerodha_nfo", settings.auto_topup_zerodha_nfo)) if on]


async def _carry_queued_jobs(db: AsyncSession, run: BfBackfillRun) -> set[tuple]:
    """Moves the still-queued jobs of earlier top-ups of `run.source` into
    `run`, extended to its session. Returns their (symbol_id, timeframe)."""
    earlier_ids = (
        await db.execute(
            select(BfBackfillJob.run_id).where(
                BfBackfillJob.source == run.source, BfBackfillJob.status == BfBackfillStatus.PENDING.value,
                BfBackfillJob.run_id.is_not(None), BfBackfillJob.run_id != run.id,
            ).distinct()
        )
    ).scalars().all()
    taken: set[tuple] = set()
    for earlier_id in earlier_ids:
        moved = (
            await db.execute(
                update(BfBackfillJob)
                .where(BfBackfillJob.run_id == earlier_id, BfBackfillJob.status == BfBackfillStatus.PENDING.value)
                .values(run_id=run.id, end_date=run.session_date)
                .returning(BfBackfillJob.symbol_id, BfBackfillJob.timeframe)
                .execution_options(synchronize_session=False)
            )
        ).all()
        taken.update((m.symbol_id, m.timeframe) for m in moved)
        earlier = await db.get(BfBackfillRun, earlier_id)
        if earlier is not None:
            earlier.jobs_total = max(0, earlier.jobs_total - len(moved))
            earlier.message = f"{len(moved):,} queued job{'s' if len(moved) != 1 else ''} moved to the {run.session_date:%d %b} top-up"
    return taken


async def queue_topup_jobs(
    db: AsyncSession, run: BfBackfillRun, timeframes: list[str], user_id, now: datetime, base_priority: int,
) -> int:
    """Adds a job for every pair of `run.source` behind the last session.

    Jobs an earlier top-up still has queued (e.g. a catch-up still going at
    16:15) move to this run and fetch through its session, instead of a
    second job for the same pair."""
    taken = await _carry_queued_jobs(db, run)
    native = {o.value for o in timeframes_for_source(run.source) if o.native}
    wanted = [tf for tf in timeframes if tf in native]
    rows = (
        await db.execute(
            select(BfCoverage, BfSymbol)
            .join(BfSymbol, BfSymbol.id == BfCoverage.symbol_id)
            .where(BfSymbol.source == run.source, BfCoverage.timeframe.in_(wanted))
        )
    ).all()
    queued = len(taken)
    for coverage, symbol in rows:
        if (symbol.id, coverage.timeframe) in taken:
            continue
        last_day = ist_date(coverage.last_ts)
        if symbol.expiry is not None and symbol.expiry <= last_day:
            continue  # expired contract: nothing trades after its last saved day
        if sessions_behind(coverage.last_ts, coverage.timeframe, now, coverage.checked_through) == 0:
            continue
        db.add(BfBackfillJob(
            symbol_id=symbol.id, source=run.source, timeframe=coverage.timeframe,
            start_date=last_day, end_date=run.session_date, requested_by=user_id, run_id=run.id,
            priority=topup_priority(base_priority, run.source, coverage.timeframe),
        ))
        queued += 1
    if run.source == "zerodha_nfo" and EMPTY_CONTRACT_TIMEFRAME in wanted:
        for symbol in (await db.execute(untraded_active_contracts(run.session_date))).scalars().all():
            if (symbol.id, EMPTY_CONTRACT_TIMEFRAME) in taken:
                continue
            db.add(BfBackfillJob(
                symbol_id=symbol.id, source=run.source, timeframe=EMPTY_CONTRACT_TIMEFRAME,
                end_date=run.session_date, requested_by=user_id, run_id=run.id,
                priority=topup_priority(base_priority, run.source, EMPTY_CONTRACT_TIMEFRAME),
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
            await self._remind_login(db, now)
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

    async def _remind_login(self, db: AsyncSession, now: datetime) -> None:
        """From 08:45 IST on a trading day, an in-app alert if Zerodha isn't
        logged in yet -- the live sync (from 09:00) and the top-up need it."""
        ist = now.astimezone(IST)
        if not is_trading_day(ist.date()) or ist.time() < LOGIN_REMINDER_AT or ist.time() > time(15, 30):
            return
        if await find_connected_zerodha_account(db) is not None:
            return
        await _alert_admins(
            db, key=f"login-{ist.date().isoformat()}", severity=AlertSeverity.WARNING, alert_type=AlertType.BROKER_DISCONNECTED.value,
            title="Log in to Zerodha",
            message="Zerodha isn't logged in yet today. Live data (09:00-15:30 IST) and the 16:15 top-up need the daily login -- Settings > Brokers.",
        )
        await db.commit()

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
            summary = f"{done:,} done, {failed:,} failed" if failed else f"{done:,} done"
            # A note left by _carry_queued_jobs stays after the counts.
            run.message = summary if not run.message else run.message if not (done or failed) else f"{summary} · {run.message}"
            if failed:
                segment = "NSE" if run.source == "zerodha" else "NFO"
                await _alert_admins(
                    db, key=f"run-{run.id}", severity=AlertSeverity.WARNING, alert_type=AlertType.DATA_DISCONNECTED.value,
                    title=f"{segment} top-up finished with {failed:,} failed job{'s' if failed != 1 else ''}",
                    message=f"{done:,} jobs completed, {failed:,} failed. See Needs attention on the Data Backfill page to re-run them.",
                )
        await db.commit()


backfill_topup_scheduler = BackfillTopupScheduler()
