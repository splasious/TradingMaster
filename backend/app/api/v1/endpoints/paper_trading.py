import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.paper_trading import (
    DeploymentStatus,
    PaperDeployment,
    PaperOrder,
    PaperPortfolio,
    PaperPosition,
    PaperTrade,
)
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.paper_trading import (
    DeploymentCreate,
    DeploymentOut,
    EvaluationOut,
    OrderOut,
    PortfolioOut,
    PositionOut,
    TradeOut,
)
from app.services.audit import write_audit_log
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.engine import evaluate_deployment
from app.services.strategy.state_machine import StrategyStatus, can_transition

router = APIRouter()


async def _get_or_create_portfolio(db: AsyncSession, user: User) -> PaperPortfolio:
    result = await db.execute(select(PaperPortfolio).where(PaperPortfolio.user_id == user.id))
    portfolio = result.scalar_one_or_none()
    if portfolio is None:
        portfolio = PaperPortfolio(user_id=user.id, cash=100000.0, initial_capital=100000.0)
        db.add(portfolio)
        await db.flush()
    return portfolio


async def _deployment_out(db: AsyncSession, deployment: PaperDeployment) -> DeploymentOut:
    strategy = await db.get(Strategy, deployment.strategy_id)
    instrument = await db.get(Instrument, deployment.instrument_id)
    position_result = await db.execute(select(PaperPosition).where(PaperPosition.deployment_id == deployment.id))
    position = position_result.scalar_one_or_none()

    position_out = None
    if position is not None:
        current_price = tick_engine.get_current_price(instrument.id)
        unrealized = (current_price - position.avg_entry_price) * position.quantity if current_price else None
        position_out = PositionOut(
            instrument_symbol=instrument.symbol, quantity=position.quantity, avg_entry_price=position.avg_entry_price,
            current_price=current_price, unrealized_pnl=unrealized, opened_at=position.opened_at,
        )

    return DeploymentOut(
        id=str(deployment.id), strategy_id=str(deployment.strategy_id), strategy_name=strategy.name,
        instrument_id=str(deployment.instrument_id), instrument_symbol=instrument.symbol, timeframe=deployment.timeframe,
        status=deployment.status, last_evaluated_at=deployment.last_evaluated_at, created_at=deployment.created_at,
        stopped_at=deployment.stopped_at, open_position=position_out,
    )


@router.post("/deployments", response_model=DeploymentOut, status_code=status.HTTP_201_CREATED)
async def start_deployment(
    payload: DeploymentCreate, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader", "analyst"))
) -> DeploymentOut:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")

    instrument = await db.get(Instrument, uuid.UUID(payload.instrument_id))
    if instrument is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    portfolio = await _get_or_create_portfolio(db, user)

    # Starting paper trading is the real, earned trigger for the
    # BACKTESTED/OPTIMIZED -> PAPER_TRADING transition (PRD section 25).
    current_status = StrategyStatus(strategy.status)
    if can_transition(current_status, StrategyStatus.PAPER_TRADING):
        strategy.status = StrategyStatus.PAPER_TRADING.value

    deployment = PaperDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        timeframe=payload.timeframe, status=DeploymentStatus.ACTIVE.value,
    )
    db.add(deployment)
    await db.flush()
    tick_engine.subscribe(instrument.id, seed_price=100.0)

    await write_audit_log(
        db, user_id=user.id, action="PAPER_TRADING_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"instrument": instrument.symbol, "timeframe": payload.timeframe},
    )
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.get("/deployments", response_model=list[DeploymentOut])
async def list_deployments(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[DeploymentOut]:
    portfolio = await _get_or_create_portfolio(db, user)
    await db.commit()
    result = await db.execute(
        select(PaperDeployment).where(PaperDeployment.portfolio_id == portfolio.id).order_by(PaperDeployment.created_at.desc())
    )
    return [await _deployment_out(db, d) for d in result.scalars().all()]


@router.post("/deployments/{deployment_id}/stop", response_model=DeploymentOut)
async def stop_deployment(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> DeploymentOut:
    deployment = await db.get(PaperDeployment, uuid.UUID(deployment_id))
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    if portfolio.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your deployment")

    deployment.status = DeploymentStatus.STOPPED.value
    deployment.stopped_at = datetime.now(timezone.utc)
    tick_engine.unsubscribe(deployment.instrument_id)
    await write_audit_log(db, user_id=user.id, action="PAPER_TRADING_STOPPED", object_type="paper_deployment", object_id=str(deployment.id))
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.post("/deployments/{deployment_id}/evaluate", response_model=EvaluationOut)
async def evaluate_deployment_now(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> EvaluationOut:
    """Manual trigger so a demo/test doesn't have to wait for the
    background scheduler's ~10s cadence -- the background loop calls the
    exact same evaluate_deployment()."""
    deployment = await db.get(PaperDeployment, uuid.UUID(deployment_id))
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    if portfolio.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your deployment")

    outcome = await evaluate_deployment(db, deployment)
    return EvaluationOut(action=outcome.action, signal=outcome.signal, price=outcome.price, reason=outcome.reason)


@router.get("/portfolio", response_model=PortfolioOut)
async def get_portfolio(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> PortfolioOut:
    portfolio = await _get_or_create_portfolio(db, user)
    await db.commit()

    deployments_result = await db.execute(select(PaperDeployment).where(PaperDeployment.portfolio_id == portfolio.id))
    deployments = list(deployments_result.scalars().all())

    positions: list[PositionOut] = []
    unrealized_total = 0.0
    for deployment in deployments:
        position_result = await db.execute(select(PaperPosition).where(PaperPosition.deployment_id == deployment.id))
        position = position_result.scalar_one_or_none()
        if position is None:
            continue
        instrument = await db.get(Instrument, deployment.instrument_id)
        current_price = tick_engine.get_current_price(instrument.id)
        unrealized = (current_price - position.avg_entry_price) * position.quantity if current_price else 0.0
        unrealized_total += unrealized
        positions.append(
            PositionOut(
                instrument_symbol=instrument.symbol, quantity=position.quantity, avg_entry_price=position.avg_entry_price,
                current_price=current_price, unrealized_pnl=unrealized, opened_at=position.opened_at,
            )
        )

    trades_result = await db.execute(
        select(PaperTrade).join(PaperDeployment).where(PaperDeployment.portfolio_id == portfolio.id)
    )
    realized_total = sum(t.pnl for t in trades_result.scalars().all())

    equity = portfolio.cash + sum(p.quantity * (p.current_price or p.avg_entry_price) for p in positions)

    return PortfolioOut(
        cash=portfolio.cash, initial_capital=portfolio.initial_capital, equity=equity, unrealized_pnl=unrealized_total,
        realized_pnl_total=realized_total, positions=positions,
    )


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(deployment_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[OrderOut]:
    result = await db.execute(
        select(PaperOrder).where(PaperOrder.deployment_id == uuid.UUID(deployment_id)).order_by(PaperOrder.created_at.desc())
    )
    return [
        OrderOut(id=str(o.id), side=o.side, quantity=o.quantity, price=o.price, status=o.status, reason=o.reason, created_at=o.created_at)
        for o in result.scalars().all()
    ]


@router.get("/trades", response_model=list[TradeOut])
async def list_trades(deployment_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[TradeOut]:
    result = await db.execute(
        select(PaperTrade).where(PaperTrade.deployment_id == uuid.UUID(deployment_id)).order_by(PaperTrade.exit_ts.desc())
    )
    return [TradeOut.model_validate(t, from_attributes=True) for t in result.scalars().all()]
