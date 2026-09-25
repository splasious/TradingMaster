"""Runs queued backfill jobs one at a time, straight from the database.

Jobs used to run as in-process BackgroundTasks: a restart (every deploy)
dropped the whole queue, and a "Backfill All" per timeframe ran several
chains at once, tripping Kite's 3-requests-a-second limit. Now anything
that wants a backfill adds a "pending" job and calls `wake()`; this one
worker takes the next job -- lowest priority number first (someone waiting
on a manual job beats the daily top-up), oldest first -- and paces itself.
A job's own retries wait in the queue until their `run_after`.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus
from app.services.backfill_platform import jobs as jobs_module

logger = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 5
# Between jobs -- keeps a run of one-request jobs under Kite's 3 a second.
PACING_SECONDS = 0.35


async def next_job_id(now: datetime | None = None, ignore_delays: bool = False) -> uuid.UUID | None:
    now = now or datetime.now(timezone.utc)
    stmt = select(BfBackfillJob.id).where(BfBackfillJob.status == BfBackfillStatus.PENDING.value)
    if not ignore_delays:
        stmt = stmt.where(or_(BfBackfillJob.run_after.is_(None), BfBackfillJob.run_after <= now))
    stmt = stmt.order_by(BfBackfillJob.priority, BfBackfillJob.created_at).limit(1)
    async with jobs_module.AsyncSessionLocal() as db:
        return (await db.execute(stmt)).scalar_one_or_none()


class BackfillWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        # Paused from the Data Backfill page: the running job finishes, the
        # rest wait. In memory -- a restart resumes.
        self.paused = False
        self.current_job_id: uuid.UUID | None = None
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

    def wake(self) -> None:
        """New jobs are queued -- start on them now rather than at the next poll."""
        self._wake.set()

    async def run_next(self, ignore_delays: bool = False) -> bool:
        job_id = await next_job_id(ignore_delays=ignore_delays)
        if job_id is None:
            return False
        self.current_job_id = job_id
        try:
            await jobs_module.run_bf_backfill_job(job_id)
        finally:
            self.current_job_id = None
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
                await asyncio.sleep(PACING_SECONDS)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass


backfill_worker = BackfillWorker()


async def drain_queue(max_jobs: int = 10_000) -> int:
    """Runs every queued job now, retries included without their back-off --
    for tests and one-off maintenance, where no worker loop is running."""
    worker = BackfillWorker()
    ran = 0
    while ran < max_jobs and await worker.run_next(ignore_delays=True):
        ran += 1
    return ran
