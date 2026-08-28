import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.backtest import OptimizationJob, OptimizationResult
from app.models.instrument import Instrument
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.optimization import OptimizationJobCreate, OptimizationJobOut, OptimizationResultOut
from app.services.audit import write_audit_log
from app.services.backtest.optimization_runner import run_optimization_job

router = APIRouter()


def _job_out(job: OptimizationJob) -> OptimizationJobOut:
    return OptimizationJobOut(
        id=str(job.id), strategy_id=str(job.strategy_id), instrument_id=str(job.instrument_id), status=job.status,
        error_message=job.error_message, created_at=job.created_at, completed_at=job.completed_at,
    )


@router.post("", response_model=OptimizationJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_optimization(
    payload: OptimizationJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> OptimizationJobOut:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")
    if strategy.code_type != "python":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Optimization is only supported for Python strategies")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    instrument = await db.get(Instrument, uuid.UUID(payload.instrument_id))
    if instrument is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found")

    job = OptimizationJob(
        strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        timeframe=payload.timeframe, initial_capital=payload.initial_capital,
        param_ranges=[r.model_dump() for r in payload.param_ranges], rank_metric=payload.rank_metric,
        requested_by=user.id,
    )
    db.add(job)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="OPTIMIZATION_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"param_ranges": job.param_ranges, "rank_metric": job.rank_metric},
    )
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_optimization_job, job.id)
    return _job_out(job)


@router.get("/{job_id}", response_model=OptimizationJobOut)
async def get_optimization(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> OptimizationJobOut:
    job = await db.get(OptimizationJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization job not found")
    return _job_out(job)


@router.get("/{job_id}/result", response_model=OptimizationResultOut)
async def get_optimization_result(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> OptimizationResultOut:
    result = await db.execute(select(OptimizationResult).where(OptimizationResult.job_id == uuid.UUID(job_id)))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization result not available yet")
    return OptimizationResultOut(runs=row.runs)
