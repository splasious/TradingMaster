import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.backtest import PortfolioOptimizationJob, PortfolioOptimizationResult
from app.models.instrument import Instrument
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.portfolio_optimization import (
    PortfolioOptimizationJobCreate,
    PortfolioOptimizationJobOut,
    PortfolioOptimizationResultOut,
)
from app.services.audit import write_audit_log
from app.services.backtest.portfolio_optimization_runner import run_portfolio_optimization_job

router = APIRouter()


def _job_out(job: PortfolioOptimizationJob) -> PortfolioOptimizationJobOut:
    return PortfolioOptimizationJobOut(
        id=str(job.id), strategy_id=str(job.strategy_id), instrument_ids=list(job.instrument_ids),
        timeframe=job.timeframe, start_date=job.start_date, end_date=job.end_date,
        initial_capital=job.initial_capital, position_size_pct=job.position_size_pct,
        max_open_positions=job.max_open_positions, param_ranges=job.param_ranges, rank_metric=job.rank_metric,
        status=job.status, error_message=job.error_message,
        created_at=job.created_at, started_at=job.started_at, completed_at=job.completed_at,
    )


@router.post("", response_model=PortfolioOptimizationJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_portfolio_optimization(
    payload: PortfolioOptimizationJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> PortfolioOptimizationJobOut:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")
    if strategy.code_type != "python":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Portfolio optimization is only supported for Python strategies")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    instrument_ids = [uuid.UUID(i) for i in payload.instrument_ids]
    found = (await db.execute(select(Instrument.id).where(Instrument.id.in_(instrument_ids)))).scalars().all()
    missing = set(instrument_ids) - set(found)
    if missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Instrument(s) not found: {', '.join(str(m) for m in missing)}")

    job = PortfolioOptimizationJob(
        strategy_id=strategy.id, strategy_version_id=version.id, instrument_ids=payload.instrument_ids,
        timeframe=payload.timeframe, start_date=payload.start_date, end_date=payload.end_date,
        initial_capital=payload.initial_capital, position_size_pct=payload.position_size_pct,
        max_open_positions=payload.max_open_positions, brokerage_pct=payload.brokerage_pct,
        slippage_pct=payload.slippage_pct, tax_pct=payload.tax_pct,
        param_ranges=[r.model_dump() for r in payload.param_ranges], rank_metric=payload.rank_metric,
        requested_by=user.id,
    )
    db.add(job)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="PORTFOLIO_OPTIMIZATION_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"instrument_count": len(payload.instrument_ids), "param_ranges": job.param_ranges, "rank_metric": job.rank_metric},
    )
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_portfolio_optimization_job, job.id)
    return _job_out(job)


@router.get("/{job_id}", response_model=PortfolioOptimizationJobOut)
async def get_portfolio_optimization(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> PortfolioOptimizationJobOut:
    job = await db.get(PortfolioOptimizationJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio optimization job not found")
    return _job_out(job)


@router.get("", response_model=list[PortfolioOptimizationJobOut])
async def list_portfolio_optimizations(
    strategy_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> list[PortfolioOptimizationJobOut]:
    result = await db.execute(
        select(PortfolioOptimizationJob)
        .where(PortfolioOptimizationJob.strategy_id == uuid.UUID(strategy_id))
        .order_by(PortfolioOptimizationJob.created_at.desc())
    )
    return [_job_out(j) for j in result.scalars().all()]


@router.get("/{job_id}/result", response_model=PortfolioOptimizationResultOut)
async def get_portfolio_optimization_result(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> PortfolioOptimizationResultOut:
    result = await db.execute(select(PortfolioOptimizationResult).where(PortfolioOptimizationResult.job_id == uuid.UUID(job_id)))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio optimization result not available yet")
    return PortfolioOptimizationResultOut(runs=row.runs)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_portfolio_optimization(job_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    job = await db.get(PortfolioOptimizationJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio optimization job not found")

    strategy = await db.get(Strategy, job.strategy_id)
    if strategy is not None and strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")

    await write_audit_log(
        db, user_id=user.id, action="PORTFOLIO_OPTIMIZATION_DELETED", object_type="portfolio_optimization_job", object_id=str(job.id),
        previous_value={"strategy_id": str(job.strategy_id)},
    )
    await db.delete(job)  # PortfolioOptimizationResult cascades via FK ondelete
    await db.commit()
