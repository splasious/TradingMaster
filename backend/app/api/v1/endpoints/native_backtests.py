import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.paper_native_trading import _enrich_trade_legs
from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.backtest import NativeBacktestJob, NativeBacktestResult, NativeBacktestTrade
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.native_backtest import (
    NativeBacktestJobCreate,
    NativeBacktestJobOut,
    NativeBacktestResultOut,
    NativeBacktestTradeOut,
)
from app.services.audit import write_audit_log
from app.services.backtest.native_runner import run_native_backtest_job

router = APIRouter()


def _job_out(job: NativeBacktestJob) -> NativeBacktestJobOut:
    return NativeBacktestJobOut(
        id=str(job.id), strategy_id=str(job.strategy_id), start_date=job.start_date, end_date=job.end_date,
        initial_capital=job.initial_capital, status=job.status, error_message=job.error_message,
        created_at=job.created_at, started_at=job.started_at, completed_at=job.completed_at,
    )


@router.post("", response_model=NativeBacktestJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_native_backtest(
    payload: NativeBacktestJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> NativeBacktestJobOut:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")
    if strategy.code_type != "native":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only Advanced Python (native) strategies backtest this way")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    job = NativeBacktestJob(
        strategy_id=strategy.id, strategy_version_id=version.id, start_date=payload.start_date, end_date=payload.end_date,
        initial_capital=payload.initial_capital, requested_by=user.id,
    )
    db.add(job)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="NATIVE_BACKTEST_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"start_date": str(payload.start_date), "end_date": str(payload.end_date)},
    )
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_native_backtest_job, job.id)
    return _job_out(job)


@router.get("/{job_id}", response_model=NativeBacktestJobOut)
async def get_native_backtest(job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> NativeBacktestJobOut:
    job = await db.get(NativeBacktestJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Native backtest job not found")
    return _job_out(job)


@router.get("", response_model=list[NativeBacktestJobOut])
async def list_native_backtests(
    strategy_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> list[NativeBacktestJobOut]:
    result = await db.execute(
        select(NativeBacktestJob).where(NativeBacktestJob.strategy_id == uuid.UUID(strategy_id)).order_by(NativeBacktestJob.created_at.desc())
    )
    return [_job_out(j) for j in result.scalars().all()]


@router.get("/{job_id}/result", response_model=NativeBacktestResultOut)
async def get_native_backtest_result(
    job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> NativeBacktestResultOut:
    result = await db.execute(select(NativeBacktestResult).where(NativeBacktestResult.job_id == uuid.UUID(job_id)))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backtest result not available yet")
    return NativeBacktestResultOut(metrics=row.metrics, equity_curve=row.equity_curve)


@router.get("/{job_id}/trades", response_model=list[NativeBacktestTradeOut])
async def get_native_backtest_trades(
    job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> list[NativeBacktestTradeOut]:
    result = await db.execute(
        select(NativeBacktestTrade).where(NativeBacktestTrade.job_id == uuid.UUID(job_id)).order_by(NativeBacktestTrade.opened_at)
    )
    trades = list(result.scalars().all())
    enriched_legs = await _enrich_trade_legs(db, trades)
    return [
        NativeBacktestTradeOut(
            id=str(t.id), opened_at=t.opened_at, closed_at=t.closed_at, legs=enriched_legs[t.id],
            pnl=t.pnl, pnl_pct=t.pnl_pct, exit_reason=t.exit_reason,
        )
        for t in trades
    ]


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_native_backtest(job_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    job = await db.get(NativeBacktestJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Native backtest job not found")

    strategy = await db.get(Strategy, job.strategy_id)
    if strategy is not None and strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")

    await write_audit_log(
        db, user_id=user.id, action="NATIVE_BACKTEST_DELETED", object_type="native_backtest_job", object_id=str(job.id),
        previous_value={"strategy_id": str(job.strategy_id)},
    )
    await db.delete(job)  # NativeBacktestResult/NativeBacktestTrade cascade via FK ondelete
    await db.commit()
