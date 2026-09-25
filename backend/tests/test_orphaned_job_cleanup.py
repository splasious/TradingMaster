from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfSymbol
from app.services.backfill_platform.jobs import MAX_ATTEMPTS, requeue_interrupted_jobs_on_startup


async def test_a_restart_puts_interrupted_jobs_back_in_the_queue(db_session: AsyncSession, monkeypatch):
    symbol = BfSymbol(source="zerodha", symbol="ORPHANTEST", display_name="Orphan Test Co")
    db_session.add(symbol)
    await db_session.flush()

    pending = BfBackfillJob(symbol_id=symbol.id, source="zerodha", timeframe="1d", status=BfBackfillStatus.PENDING.value)
    running = BfBackfillJob(symbol_id=symbol.id, source="zerodha", timeframe="1d", status=BfBackfillStatus.RUNNING.value, attempts=1)
    worn_out = BfBackfillJob(
        symbol_id=symbol.id, source="zerodha", timeframe="1d", status=BfBackfillStatus.RUNNING.value, attempts=MAX_ATTEMPTS,
    )
    completed = BfBackfillJob(
        symbol_id=symbol.id, source="zerodha", timeframe="1d", status=BfBackfillStatus.COMPLETED.value,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add_all([pending, running, worn_out, completed])
    await db_session.commit()

    import app.services.backfill_platform.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "AsyncSessionLocal", lambda: _SessionCtx(db_session))
    count = await requeue_interrupted_jobs_on_startup()

    assert count == 2

    async def status_of(job):
        return (await db_session.execute(select(BfBackfillJob).where(BfBackfillJob.id == job.id))).scalar_one()

    assert (await status_of(pending)).status == BfBackfillStatus.PENDING.value  # still queued
    assert (await status_of(running)).status == BfBackfillStatus.PENDING.value  # resumes
    stopped = await status_of(worn_out)
    assert stopped.status == BfBackfillStatus.FAILED.value  # a job that keeps dying doesn't loop forever
    assert "restart" in stopped.error_message.lower()
    assert (await status_of(completed)).status == BfBackfillStatus.COMPLETED.value  # untouched


async def test_requeue_is_a_no_op_when_nothing_was_running(db_session: AsyncSession, monkeypatch):
    import app.services.backfill_platform.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "AsyncSessionLocal", lambda: _SessionCtx(db_session))
    count = await requeue_interrupted_jobs_on_startup()

    assert count == 0


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> None:
        return None
