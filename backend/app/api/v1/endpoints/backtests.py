import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.backtest import BacktestJob, BacktestResult, BacktestTrade
from app.models.instrument import Instrument
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.backtest import BacktestJobCreate, BacktestJobOut, BacktestResultOut, BacktestTradeOut
from app.services.audit import write_audit_log
from app.services.backtest.runner import run_backtest_job

router = APIRouter()


def _job_out(job: BacktestJob) -> BacktestJobOut:
    return BacktestJobOut(
        id=str(job.id), strategy_id=str(job.strategy_id), instrument_id=str(job.instrument_id),
        timeframe=job.timeframe, initial_capital=job.initial_capital, status=job.status,
        error_message=job.error_message, created_at=job.created_at, started_at=job.started_at,
        completed_at=job.completed_at,
    )


@router.post("", response_model=BacktestJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_backtest(
    payload: BacktestJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> BacktestJobOut:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    instrument = await db.get(Instrument, uuid.UUID(payload.instrument_id))
    if instrument is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found")

    job = BacktestJob(
        strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        timeframe=payload.timeframe, initial_capital=payload.initial_capital, brokerage_pct=payload.brokerage_pct,
        slippage_pct=payload.slippage_pct, tax_pct=payload.tax_pct,
        out_of_sample_split_pct=payload.out_of_sample_split_pct, run_monte_carlo=payload.run_monte_carlo,
        requested_by=user.id,
    )
    db.add(job)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="BACKTEST_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"instrument": instrument.symbol, "timeframe": payload.timeframe},
    )
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_backtest_job, job.id)
    return _job_out(job)


@router.get("/{job_id}", response_model=BacktestJobOut)
async def get_backtest(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> BacktestJobOut:
    job = await db.get(BacktestJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest job not found")
    return _job_out(job)


@router.get("", response_model=list[BacktestJobOut])
async def list_backtests(
    strategy_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> list[BacktestJobOut]:
    result = await db.execute(
        select(BacktestJob).where(BacktestJob.strategy_id == uuid.UUID(strategy_id)).order_by(BacktestJob.created_at.desc())
    )
    return [_job_out(j) for j in result.scalars().all()]


@router.get("/{job_id}/result", response_model=BacktestResultOut)
async def get_backtest_result(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> BacktestResultOut:
    result = await db.execute(select(BacktestResult).where(BacktestResult.job_id == uuid.UUID(job_id)))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest result not available yet")
    return BacktestResultOut(
        metrics=row.metrics, out_of_sample_metrics=row.out_of_sample_metrics, monte_carlo=row.monte_carlo,
        equity_curve=row.equity_curve,
    )


@router.get("/{job_id}/trades", response_model=list[BacktestTradeOut])
async def get_backtest_trades(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[BacktestTradeOut]:
    result = await db.execute(
        select(BacktestTrade).where(BacktestTrade.job_id == uuid.UUID(job_id)).order_by(BacktestTrade.entry_ts)
    )
    return [BacktestTradeOut.model_validate(t, from_attributes=True) for t in result.scalars().all()]
