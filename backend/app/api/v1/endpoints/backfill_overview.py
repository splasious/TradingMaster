"""The Data Backfill page's overview, the "saved up to" status for the
Dashboard card and top-bar pill, and the controls behind them: schedule,
"Top up now", pause/resume, cancel, retry failed jobs."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.backfill_platform import JOB_PRIORITY_BULK, BfBackfillRun
from app.models.user import User
from app.schemas.backfill_platform import BfRetryFailedIn, BfScheduleIn, BfTopupIn
from app.services.audit import write_audit_log
from app.services.backfill_platform import overview as overview_service
from app.services.backfill_platform.coverage import get_settings
from app.services.backfill_platform.jobs import requeue_failed
from app.services.backfill_platform.topup import RUN_RUNNING, ZerodhaNotConnected, cancel_run, start_manual_topup
from app.services.backfill_platform.worker import backfill_worker

router = APIRouter()

_OPERATORS = ("administrator", "trader", "analyst")


@router.get("/overview")
async def get_overview(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return await overview_service.build_overview(db)


@router.get("/freshness")
async def get_freshness(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    """The compact "Zerodha data saved up to" status -- Dashboard card and
    the top-bar pill on every page."""
    o = await overview_service.build_overview(db)
    nse, nfo = o["segments"][0], o["segments"][1]
    return {
        "as_of": o["as_of"],
        "coverage_ready": o["coverage_ready"],
        "last_session": o["last_session"],
        "headline": o["headline"],
        "timeframes": nse["cells"],
        "live_today_until": o["live_today_until"],
        "zerodha_login": o["zerodha_login"],
        "queue": {"state": o["queue"]["state"], "percent": (o["queue"]["run"] or {}).get("percent")},
        "next_run_at": o["schedule"]["next_run_at"],
        "delta_paused": not o["schedule"]["delta_enabled"],
        "symbols": {"zerodha": nse["symbols"], "zerodha_nfo": nfo["symbols"]},
    }


@router.get("/coverage/stocks")
async def get_coverage_stocks(
    source: str = Query("zerodha", pattern="^(zerodha|zerodha_nfo)$"),
    status_filter: str = Query("all", alias="status", pattern="^(all|current|behind|partial|failed)$"),
    q: str = Query("", max_length=50),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await overview_service.build_stocks(db, source, status_filter, q, offset, limit)


@router.get("/schedule")
async def get_schedule(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    return overview_service.schedule_out(await get_settings(db), datetime.now(timezone.utc))


@router.put("/schedule")
async def put_schedule(
    payload: BfScheduleIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator")),
) -> dict[str, Any]:
    settings = await get_settings(db)
    before = overview_service.schedule_out(settings, datetime.now(timezone.utc))
    for field, value in payload.model_dump().items():
        setattr(settings, field, value)
    await write_audit_log(
        db, user_id=user.id, action="BF_SCHEDULE_UPDATED", object_type="bf_settings", object_id="1",
        previous_value={k: before[k] for k in payload.model_dump()}, new_value=payload.model_dump(),
    )
    await db.commit()
    overview_service.clear_cache()
    return overview_service.schedule_out(settings, datetime.now(timezone.utc))


@router.post("/topup", status_code=status.HTTP_202_ACCEPTED)
async def post_topup(
    payload: BfTopupIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_role(*_OPERATORS)),
) -> dict[str, Any]:
    try:
        runs = await start_manual_topup(db, user.id, payload.sources)
    except ZerodhaNotConnected as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await write_audit_log(
        db, user_id=user.id, action="BF_TOPUP_STARTED", object_type="bf_backfill_run", object_id=",".join(str(r.id) for r in runs),
        new_value={r.source: r.jobs_total for r in runs},
    )
    await db.commit()
    overview_service.clear_cache()
    return {"queued": {r.source: r.jobs_total for r in runs}}


@router.post("/runs/cancel")
async def post_cancel_runs(db: AsyncSession = Depends(get_db), user: User = Depends(require_role(*_OPERATORS))) -> dict[str, Any]:
    runs = (await db.execute(select(BfBackfillRun).where(BfBackfillRun.status == RUN_RUNNING))).scalars().all()
    cancelled = 0
    for run in runs:
        cancelled += await cancel_run(db, run)
    await write_audit_log(db, user_id=user.id, action="BF_TOPUP_CANCELLED", object_type="bf_backfill_run", new_value={"jobs_cancelled": cancelled})
    await db.commit()
    overview_service.clear_cache()
    return {"runs": len(runs), "jobs_cancelled": cancelled}


@router.post("/worker/{action}")
async def post_worker(action: str, user: User = Depends(require_role(*_OPERATORS))) -> dict[str, Any]:
    if action not in ("pause", "resume"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown action")
    backfill_worker.paused = action == "pause"
    if not backfill_worker.paused:
        backfill_worker.wake()
    overview_service.clear_cache()
    return {"paused": backfill_worker.paused}


@router.post("/jobs/retry-failed")
async def post_retry_failed(
    payload: BfRetryFailedIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_role(*_OPERATORS)),
) -> dict[str, Any]:
    count = await requeue_failed(db, payload.kind, JOB_PRIORITY_BULK)
    await write_audit_log(db, user_id=user.id, action="BF_FAILED_JOBS_RETRIED", object_type="bf_backfill_job", new_value={"kind": payload.kind, "count": count})
    await db.commit()
    backfill_worker.wake()
    overview_service.clear_cache()
    return {"requeued": count}
