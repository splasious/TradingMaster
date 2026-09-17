import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.paper_trading import (
    NativeDeploymentCreate,
    NativeDeploymentOut,
    NativeEvaluationOut,
    NativeLegOut,
    NativePositionOut,
    NativeTradeOut,
)
from app.services.audit import write_audit_log
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import exit_native_deployment_now, run_native_strategy
from app.services.strategy.state_machine import StrategyStatus, can_transition

router = APIRouter()


async def _build_position_out(db: AsyncSession, state: dict | None) -> NativePositionOut | None:
    """Display-only reconstruction of the short/long-leg credit-spread
    shape (see NativePositionOut's docstring) from a deployment's raw
    state blob -- returns None for any state that isn't shaped this way
    (flat, or a future native strategy with a different convention)
    rather than guessing."""
    position = (state or {}).get("position") if state else None
    if not isinstance(position, dict) or "short" not in position or "long" not in position:
        return None

    legs_out: list[NativeLegOut] = []
    prices: dict[str, float | None] = {}
    for side in ("short", "long"):
        leg = position[side]
        instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        if instrument is None:
            return None
        current_price = tick_engine.get_current_price(instrument.id)
        prices[side] = current_price
        legs_out.append(
            NativeLegOut(
                instrument_symbol=instrument.symbol, strike=instrument.strike, option_type=instrument.option_type,
                side=side, quantity=leg["quantity"], entry_price=leg["entry_price"], current_price=current_price,
            )
        )

    short_leg, long_leg = position["short"], position["long"]
    trade_value = (short_leg["entry_price"] - long_leg["entry_price"]) * short_leg["quantity"]
    live_value = None
    unrealized_pnl = None
    if prices["short"] is not None and prices["long"] is not None:
        live_value = (prices["short"] - prices["long"]) * short_leg["quantity"]
        unrealized_pnl = trade_value - live_value

    return NativePositionOut(
        bias=position.get("bias"), opened_at=datetime.fromisoformat(position["opened_at"]),
        legs=legs_out, trade_value=trade_value, live_value=live_value, unrealized_pnl=unrealized_pnl,
    )


async def _get_owned_portfolio(db: AsyncSession, user: User, portfolio_id: str) -> PaperPortfolio:
    portfolio = await db.get(PaperPortfolio, uuid.UUID(portfolio_id))
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capital pool not found")
    if portfolio.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your capital pool")
    return portfolio


async def _get_owned_native_deployment(db: AsyncSession, user: User, deployment_id: str) -> tuple[PaperNativeDeployment, PaperPortfolio]:
    deployment = await db.get(PaperNativeDeployment, uuid.UUID(deployment_id))
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    if portfolio.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your deployment")
    return deployment, portfolio


async def _deployment_out(db: AsyncSession, deployment: PaperNativeDeployment) -> NativeDeploymentOut:
    strategy = await db.get(Strategy, deployment.strategy_id)
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    position = await _build_position_out(db, deployment.state)
    return NativeDeploymentOut(
        id=str(deployment.id), strategy_id=str(deployment.strategy_id), strategy_name=strategy.name,
        portfolio_id=str(portfolio.id), portfolio_name=portfolio.name, currency=portfolio.currency,
        status=deployment.status, last_evaluated_at=deployment.last_evaluated_at,
        last_signal=deployment.last_signal, last_signal_reason=deployment.last_signal_reason,
        state=deployment.state, position=position, created_at=deployment.created_at, stopped_at=deployment.stopped_at,
    )


async def _deployment_outs_batch(db: AsyncSession, deployments: list[PaperNativeDeployment]) -> list[NativeDeploymentOut]:
    if not deployments:
        return []
    strategy_ids = {d.strategy_id for d in deployments}
    portfolio_ids = {d.portfolio_id for d in deployments}
    strategies = {s.id: s for s in (await db.execute(select(Strategy).where(Strategy.id.in_(strategy_ids)))).scalars()}
    portfolios = {p.id: p for p in (await db.execute(select(PaperPortfolio).where(PaperPortfolio.id.in_(portfolio_ids)))).scalars()}
    out: list[NativeDeploymentOut] = []
    for d in deployments:
        position = await _build_position_out(db, d.state)
        out.append(
            NativeDeploymentOut(
                id=str(d.id), strategy_id=str(d.strategy_id), strategy_name=strategies[d.strategy_id].name,
                portfolio_id=str(d.portfolio_id), portfolio_name=portfolios[d.portfolio_id].name,
                currency=portfolios[d.portfolio_id].currency, status=d.status, last_evaluated_at=d.last_evaluated_at,
                last_signal=d.last_signal, last_signal_reason=d.last_signal_reason, state=d.state,
                position=position, created_at=d.created_at, stopped_at=d.stopped_at,
            )
        )
    return out


@router.post("/native-deployments", response_model=NativeDeploymentOut, status_code=status.HTTP_201_CREATED)
async def start_native_deployment(
    payload: NativeDeploymentCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> NativeDeploymentOut:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")
    if strategy.code_type != "native":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only Advanced Python (native) strategies deploy this way")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    portfolio = await _get_owned_portfolio(db, user, payload.portfolio_id)

    # Native strategies have no backtest step to earn -- they can't run
    # through the single-instrument backtest engine at all (see
    # native_runner.py's docstring: no fixed instrument, real DB access,
    # multi-leg positions, none of which generate_signal(candles, params)
    # can express). Starting paper trading is the first real, earned
    # milestone available for this strategy type, so DRAFT -> PAPER_TRADING
    # is allowed directly here rather than via the general state machine,
    # which stays strict -- "nothing fabricates a backtested strategy" --
    # for every other strategy type.
    current_status = StrategyStatus(strategy.status)
    if current_status == StrategyStatus.DRAFT:
        strategy.status = StrategyStatus.PAPER_TRADING.value
    elif can_transition(current_status, StrategyStatus.PAPER_TRADING):
        strategy.status = StrategyStatus.PAPER_TRADING.value

    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
        status=DeploymentStatus.ACTIVE.value, state=None,
    )
    db.add(deployment)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="PAPER_NATIVE_TRADING_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"portfolio_id": str(portfolio.id)},
    )
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.get("/native-deployments", response_model=list[NativeDeploymentOut])
async def list_native_deployments(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[NativeDeploymentOut]:
    result = await db.execute(
        select(PaperNativeDeployment)
        .join(PaperPortfolio, PaperNativeDeployment.portfolio_id == PaperPortfolio.id)
        .where(PaperPortfolio.user_id == user.id)
        .order_by(PaperNativeDeployment.created_at.desc())
    )
    return await _deployment_outs_batch(db, list(result.scalars().all()))


@router.post("/native-deployments/{deployment_id}/stop", response_model=NativeDeploymentOut)
async def stop_native_deployment(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> NativeDeploymentOut:
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    deployment.status = DeploymentStatus.STOPPED.value
    deployment.stopped_at = datetime.now(timezone.utc)
    await write_audit_log(db, user_id=user.id, action="PAPER_NATIVE_TRADING_STOPPED", object_type="paper_native_deployment", object_id=str(deployment.id))
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.delete("/native-deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_native_deployment(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    if deployment.status == DeploymentStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stop this deployment before deleting it.")

    await db.execute(delete(PaperNativeTrade).where(PaperNativeTrade.deployment_id == deployment.id))
    await write_audit_log(
        db, user_id=user.id, action="PAPER_NATIVE_DEPLOYMENT_DELETED", object_type="paper_native_deployment", object_id=str(deployment.id),
    )
    await db.delete(deployment)
    await db.commit()


@router.post("/native-deployments/{deployment_id}/evaluate", response_model=NativeEvaluationOut)
async def evaluate_native_deployment_now(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> NativeEvaluationOut:
    """Manual trigger, same shape as the single-instrument engine's
    /evaluate -- the background scheduler calls the exact same
    run_native_strategy()."""
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    outcome = await run_native_strategy(db, deployment)
    return NativeEvaluationOut(action=outcome.action, signal=outcome.signal, reason=outcome.reason)


@router.post("/native-deployments/{deployment_id}/exit", response_model=NativeEvaluationOut)
async def exit_native_deployment_now_endpoint(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> NativeEvaluationOut:
    """Asks the strategy's own evaluate() to force-close whatever it
    currently holds (state["force_exit"]) rather than trying to unwind an
    arbitrary legs shape generically from outside the strategy's code."""
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    outcome = await exit_native_deployment_now(db, deployment)
    return NativeEvaluationOut(action=outcome.action, signal=outcome.signal, reason=outcome.reason)


@router.get("/native-trades", response_model=list[NativeTradeOut])
async def list_native_trades(
    deployment_id: str | None = None, limit: int = 200,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> list[NativeTradeOut]:
    if deployment_id:
        result = await db.execute(
            select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == uuid.UUID(deployment_id)).order_by(PaperNativeTrade.closed_at.desc())
        )
        return [
            NativeTradeOut(
                id=str(t.id), deployment_id=str(t.deployment_id), opened_at=t.opened_at, closed_at=t.closed_at,
                legs=t.legs, pnl=t.pnl, pnl_pct=t.pnl_pct, exit_reason=t.exit_reason,
            )
            for t in result.scalars().all()
        ]

    result = await db.execute(
        select(PaperNativeTrade, Strategy.name)
        .join(PaperNativeDeployment, PaperNativeTrade.deployment_id == PaperNativeDeployment.id)
        .join(PaperPortfolio, PaperNativeDeployment.portfolio_id == PaperPortfolio.id)
        .join(Strategy, PaperNativeDeployment.strategy_id == Strategy.id)
        .where(PaperPortfolio.user_id == user.id)
        .order_by(PaperNativeTrade.closed_at.desc())
        .limit(limit)
    )
    return [
        NativeTradeOut(
            id=str(t.id), deployment_id=str(t.deployment_id), strategy_name=name, opened_at=t.opened_at, closed_at=t.closed_at,
            legs=t.legs, pnl=t.pnl, pnl_pct=t.pnl_pct, exit_reason=t.exit_reason,
        )
        for t, name in result.all()
    ]
