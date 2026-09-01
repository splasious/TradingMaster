import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
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
    PortfolioCreate,
    PortfolioOut,
    PortfolioUpdate,
    PositionOut,
    TradeOut,
)
from app.services.audit import write_audit_log
from app.services.market_data.seed_price import get_seed_price
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.engine import evaluate_deployment
from app.services.strategy.state_machine import StrategyStatus, can_transition

router = APIRouter()


async def _list_portfolios(db: AsyncSession, user: User) -> list[PaperPortfolio]:
    result = await db.execute(select(PaperPortfolio).where(PaperPortfolio.user_id == user.id).order_by(PaperPortfolio.created_at))
    portfolios = list(result.scalars().all())
    if not portfolios:
        # First-time UX is unchanged: a brand-new user gets one ready-to-use
        # pool instead of an empty screen and a mandatory create-pool step.
        default = PaperPortfolio(user_id=user.id, name="Default", currency="INR", cash=100000.0, initial_capital=100000.0)
        db.add(default)
        await db.flush()
        portfolios = [default]
    return portfolios


async def _get_owned_portfolio(db: AsyncSession, user: User, portfolio_id: str) -> PaperPortfolio:
    portfolio = await db.get(PaperPortfolio, uuid.UUID(portfolio_id))
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capital pool not found")
    if portfolio.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your capital pool")
    return portfolio


async def _portfolio_out(db: AsyncSession, portfolio: PaperPortfolio) -> PortfolioOut:
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
        id=str(portfolio.id), name=portfolio.name, currency=portfolio.currency,
        cash=portfolio.cash, initial_capital=portfolio.initial_capital, equity=equity, unrealized_pnl=unrealized_total,
        realized_pnl_total=realized_total, positions=positions,
    )


async def _deployment_out(db: AsyncSession, deployment: PaperDeployment) -> DeploymentOut:
    strategy = await db.get(Strategy, deployment.strategy_id)
    instrument = await db.get(Instrument, deployment.instrument_id)
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
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
        instrument_id=str(deployment.instrument_id), instrument_symbol=instrument.symbol,
        portfolio_id=str(portfolio.id), portfolio_name=portfolio.name, currency=portfolio.currency,
        timeframe=deployment.timeframe, status=deployment.status, last_evaluated_at=deployment.last_evaluated_at,
        created_at=deployment.created_at, stopped_at=deployment.stopped_at, open_position=position_out,
    )


@router.post("/portfolios", response_model=PortfolioOut, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    payload: PortfolioCreate, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader", "analyst"))
) -> PortfolioOut:
    """A named, currency-scoped capital pool -- e.g. one INR pool for NSE
    strategies, one USD pool for Delta Exchange strategies, tracked
    independently with no FX conversion between them (PRD section 21)."""
    portfolio = PaperPortfolio(
        user_id=user.id, name=payload.name, currency=payload.currency,
        cash=payload.initial_capital, initial_capital=payload.initial_capital,
    )
    db.add(portfolio)
    await db.flush()
    await write_audit_log(
        db, user_id=user.id, action="PAPER_PORTFOLIO_CREATED", object_type="paper_portfolio", object_id=str(portfolio.id),
        new_value={"name": payload.name, "currency": payload.currency, "initial_capital": payload.initial_capital},
    )
    await db.commit()
    return await _portfolio_out(db, portfolio)


@router.get("/portfolios", response_model=list[PortfolioOut])
async def list_portfolios(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[PortfolioOut]:
    portfolios = await _list_portfolios(db, user)
    await db.commit()
    return [await _portfolio_out(db, p) for p in portfolios]


@router.patch("/portfolios/{portfolio_id}", response_model=PortfolioOut)
async def update_portfolio(
    portfolio_id: str, payload: PortfolioUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> PortfolioOut:
    """Resets both cash and initial_capital to the given amount -- a real
    reset, not just relabeling the starting baseline, since paper trading's
    whole point is a clean simulation the user can size however they want
    before (or between) runs. Doesn't touch existing deployments/positions/
    trade history; equity just recomputes from the new cash on next read."""
    portfolio = await _get_owned_portfolio(db, user, portfolio_id)
    portfolio.cash = payload.initial_capital
    portfolio.initial_capital = payload.initial_capital
    if payload.name:
        portfolio.name = payload.name
    await write_audit_log(
        db, user_id=user.id, action="PAPER_PORTFOLIO_CAPITAL_RESET", object_type="paper_portfolio", object_id=str(portfolio.id),
        new_value={"initial_capital": payload.initial_capital, "name": payload.name},
    )
    await db.commit()
    return await _portfolio_out(db, portfolio)


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

    portfolio = await _get_owned_portfolio(db, user, payload.portfolio_id)

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
    tick_engine.subscribe(instrument.id, seed_price=await get_seed_price(db, instrument.id))

    await write_audit_log(
        db, user_id=user.id, action="PAPER_TRADING_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"instrument": instrument.symbol, "timeframe": payload.timeframe, "portfolio_id": str(portfolio.id)},
    )
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.get("/deployments", response_model=list[DeploymentOut])
async def list_deployments(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[DeploymentOut]:
    await _list_portfolios(db, user)  # ensures a default pool exists for a brand-new user
    await db.commit()
    result = await db.execute(
        select(PaperDeployment)
        .join(PaperPortfolio, PaperDeployment.portfolio_id == PaperPortfolio.id)
        .where(PaperPortfolio.user_id == user.id)
        .order_by(PaperDeployment.created_at.desc())
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


@router.delete("/deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deployment(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    """Stopped deployments only -- deleting an active one would silently
    drop its tick-engine subscription and any open position. Explicitly
    deletes child orders/trades/position first: ON DELETE CASCADE is
    declared on all three FKs, but SQLite (local dev) doesn't enforce it
    without a per-connection PRAGMA, so this behaves identically on
    SQLite and Postgres rather than depending on which DB is behind it."""
    deployment = await db.get(PaperDeployment, uuid.UUID(deployment_id))
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    if portfolio.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your deployment")
    if deployment.status == DeploymentStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stop this deployment before deleting it.")

    await db.execute(delete(PaperTrade).where(PaperTrade.deployment_id == deployment.id))
    await db.execute(delete(PaperOrder).where(PaperOrder.deployment_id == deployment.id))
    await db.execute(delete(PaperPosition).where(PaperPosition.deployment_id == deployment.id))

    await write_audit_log(
        db, user_id=user.id, action="PAPER_DEPLOYMENT_DELETED", object_type="paper_deployment", object_id=str(deployment.id),
    )
    await db.delete(deployment)
    await db.commit()


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
