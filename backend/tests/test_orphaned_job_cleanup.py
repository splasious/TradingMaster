from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfSymbol
from app.services.backfill_platform.jobs import fail_orphaned_jobs_on_startup


async def test_fail_orphaned_jobs_marks_pending_and_running_as_failed(db_session: AsyncSession, monkeypatch):
    symbol = BfSymbol(source="yahoo", symbol="ORPHANTEST", display_name="Orphan Test Co")
    db_session.add(symbol)
    await db_session.flush()

    pending = BfBackfillJob(symbol_id=symbol.id, source="yahoo", timeframe="1d", status=BfBackfillStatus.PENDING.value)
    running = BfBackfillJob(symbol_id=symbol.id, source="yahoo", timeframe="1d", status=BfBackfillStatus.RUNNING.value)
    completed = BfBackfillJob(
        symbol_id=symbol.id, source="yahoo", timeframe="1d", status=BfBackfillStatus.COMPLETED.value,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add_all([pending, running, completed])
    await db_session.commit()

    import app.services.backfill_platform.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "AsyncSessionLocal", lambda: _SessionCtx(db_session))
    count = await fail_orphaned_jobs_on_startup()

    assert count == 2

    refreshed_pending = (await db_session.execute(select(BfBackfillJob).where(BfBackfillJob.id == pending.id))).scalar_one()
    refreshed_running = (await db_session.execute(select(BfBackfillJob).where(BfBackfillJob.id == running.id))).scalar_one()
    refreshed_completed = (await db_session.execute(select(BfBackfillJob).where(BfBackfillJob.id == completed.id))).scalar_one()

    assert refreshed_pending.status == BfBackfillStatus.FAILED.value
    assert "restart" in refreshed_pending.error_message.lower()
    assert refreshed_running.status == BfBackfillStatus.FAILED.value
    assert refreshed_completed.status == BfBackfillStatus.COMPLETED.value  # untouched


async def test_fail_orphaned_jobs_no_op_when_none_pending(db_session: AsyncSession, monkeypatch):
    import app.services.backfill_platform.jobs as jobs_module

    monkeypatch.setattr(jobs_module, "AsyncSessionLocal", lambda: _SessionCtx(db_session))
    count = await fail_orphaned_jobs_on_startup()

    assert count == 0


class _SessionCtx:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> None:
        return None
