"""Runs queued backfill jobs straight from the database, a few at a time.

Jobs used to run as in-process BackgroundTasks: a restart (every deploy)
dropped the whole queue, and a "Backfill All" per timeframe ran several
chains at once, tripping Kite's 3-requests-a-second limit. Now anything
that wants a backfill adds a "pending" job and calls `wake()`; this worker
takes the next job -- lowest priority number first (someone waiting on a
manual job beats the daily top-up), oldest first. It runs
settings.backfill_parallel of them at once, so one waiting on Kite's answer
doesn't hold the rest up; every Kite request still queues on the app-wide
limit of 3 a second (jobs.py). A job's own retries wait in the queue until
their `run_after`.
"""

import asyncio
import logging
import uuid
from collections.abc import Collection
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus
from app.services.backfill_platform import jobs as jobs_module

logger = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 5
MAX_PARALLEL = 5


async def next_job_id(
    now: datetime | None = None, ignore_delays: bool = False, exclude: Collection[uuid.UUID] = (),
) -> uuid.UUID | None:
    now = now or datetime.now(timezone.utc)
    stmt = select(BfBackfillJob.id).where(BfBackfillJob.status == BfBackfillStatus.PENDING.value)
    if not ignore_delays:
        stmt = stmt.where(or_(BfBackfillJob.run_after.is_(None), BfBackfillJob.run_after <= now))
    if exclude:
        stmt = stmt.where(BfBackfillJob.id.not_in(list(exclude)))
    stmt = stmt.order_by(BfBackfillJob.priority, BfBackfillJob.created_at).limit(1)
    async with jobs_module.AsyncSessionLocal() as db:
        return (await db.execute(stmt)).scalar_one_or_none()


class BackfillWorker:
    def __init__(self, parallel: int | None = None) -> None:
        self._parallel = parallel
        self._tasks: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self._claim = asyncio.Lock()
        # Jobs taken and not finished, in the order they started. A job is
        # added here before it is marked running, so no two slots take it.
        self._running: dict[uuid.UUID, None] = {}
        # Paused from the Data Backfill page: running jobs finish, the rest
        # wait. In memory -- a restart resumes.
        self.paused = False
        self.last_error: str | None = None

    @property
    def parallel(self) -> int:
        wanted = self._parallel if self._parallel is not None else get_settings().backfill_parallel
        return max(1, min(MAX_PARALLEL, wanted))

    @property
    def current_job_id(self) -> uuid.UUID | None:
        """The job started most recently of those running."""
        return next(reversed(self._running), None)

    @property
    def running_job_ids(self) -> list[uuid.UUID]:
        return list(self._running)

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._run()) for _ in range(self.parallel)]

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    def wake(self) -> None:
        """New jobs are queued -- start on them now rather than at the next poll."""
        self._wake.set()

    async def run_next(self, ignore_delays: bool = False) -> bool:
        async with self._claim:
            job_id = await next_job_id(ignore_delays=ignore_delays, exclude=self._running.keys())
            if job_id is None:
                return False
            self._running[job_id] = None
        try:
            await jobs_module.run_bf_backfill_job(job_id)
        finally:
            self._running.pop(job_id, None)
        return True

    async def _run(self) -> None:
        while True:
            ran = False
            try:
                if not self.paused:
                    ran = await self.run_next()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Backfill worker failed to pick up a job")
                self.last_error = str(exc)
            if ran:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass


backfill_worker = BackfillWorker()


async def drain_queue(max_jobs: int = 10_000) -> int:
    """Runs every queued job now, one after another, retries included
    without their back-off -- for tests and one-off maintenance, where no
    worker loop is running."""
    worker = BackfillWorker(parallel=1)
    ran = 0
    while ran < max_jobs and await worker.run_next(ignore_delays=True):
        ran += 1
    return ran
