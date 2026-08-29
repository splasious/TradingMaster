import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.broker import BrokerAccount
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LiveOrder, LivePosition
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.live_trading import (
    DeploymentCreate,
    EvaluationOut,
    KillSwitchActivate,
    KillSwitchOut,
    LiveDeploymentOut,
    LiveOrderOut,
    LivePositionOut,
    ReconciliationOut,
    SafetyCheckOut,
)
from app.services.audit import write_audit_log
from app.services.live_trading import kill_switch as kill_switch_service
from app.services.live_trading.oms import evaluate_live_deployment
from app.services.live_trading.reconciliation import reconcile_positions
from app.services.live_trading.safety import check_live_trading_readiness
from app.services.strategy.state_machine import StrategyStatus, can_transition

router = APIRouter()


async def _latest_version(db: AsyncSession, strategy_id: uuid.UUID) -> StrategyVersion | None:
    result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _deployment_out(db: AsyncSession, deployment: LiveDeployment) -> LiveDeploymentOut:
    strategy = await db.get(Strategy, deployment.strategy_id)
    instrument = await db.get(Instrument, deployment.instrument_id)
    position_result = await db.execute(select(LivePosition).where(LivePosition.deployment_id == deployment.id))
    position = position_result.scalar_one_or_none()

    return LiveDeploymentOut(
        id=str(deployment.id), strategy_id=str(deployment.strategy_id), strategy_name=strategy.name,
        instrument_id=str(deployment.instrument_id), instrument_symbol=instrument.symbol,
        broker_account_id=str(deployment.broker_account_id), timeframe=deployment.timeframe, status=deployment.status,
        last_evaluated_at=deployment.last_evaluated_at, created_at=deployment.created_at, stopped_at=deployment.stopped_at,
        open_position=(
            LivePositionOut(
                instrument_symbol=instrument.symbol, quantity=position.quantity, avg_entry_price=position.avg_entry_price,
                opened_at=position.opened_at,
            )
            if position
            else None
        ),
    )


@router.get("/safety-check", response_model=SafetyCheckOut)
async def safety_check(
    strategy_id: str, broker_account_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SafetyCheckOut:
    strategy = await db.get(Strategy, uuid.UUID(strategy_id))
    broker_account = await db.get(BrokerAccount, uuid.UUID(broker_account_id))
    if strategy is None or broker_account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy or broker account not found")
    version = await _latest_version(db, strategy.id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    result = await check_live_trading_readiness(db, strategy, version, broker_account)
    return SafetyCheckOut(passed=result.passed, checks=result.checks, failures=result.failures)


@router.post("/deployments", response_model=LiveDeploymentOut, status_code=status.HTTP_201_CREATED)
async def start_live_deployment(
    payload: DeploymentCreate, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader"))
) -> LiveDeploymentOut:
    if not payload.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Live trading requires explicit confirmation (confirmed=true) -- PRD section 49.",
        )

    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")

    instrument = await db.get(Instrument, uuid.UUID(payload.instrument_id))
    broker_account = await db.get(BrokerAccount, uuid.UUID(payload.broker_account_id))
    if instrument is None or broker_account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument or broker account not found")
    if broker_account.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your broker account")

    version = await _latest_version(db, strategy.id)
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    readiness = await check_live_trading_readiness(db, strategy, version, broker_account)
    if not readiness.passed:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"message": "Live trading safety checklist failed", "failures": readiness.failures},
        )

    current_status = StrategyStatus(strategy.status)
    if not can_transition(current_status, StrategyStatus.LIVE):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot go live from status '{strategy.status}'")
    strategy.status = StrategyStatus.LIVE.value

    deployment = LiveDeployment(
        owner_id=user.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        broker_account_id=broker_account.id, timeframe=payload.timeframe, status="active",
    )
    db.add(deployment)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="LIVE_TRADING_ACTIVATED", object_type="strategy", object_id=str(strategy.id),
        new_value={"instrument": instrument.symbol, "broker_account_id": str(broker_account.id)},
    )
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.get("/deployments", response_model=list[LiveDeploymentOut])
async def list_live_deployments(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[LiveDeploymentOut]:
    stmt = select(LiveDeployment)
    if "administrator" not in user.role_names:
        stmt = stmt.where(LiveDeployment.owner_id == user.id)
    result = await db.execute(stmt.order_by(LiveDeployment.created_at.desc()))
    return [await _deployment_out(db, d) for d in result.scalars().all()]


@router.post("/deployments/{deployment_id}/stop", response_model=LiveDeploymentOut)
async def stop_live_deployment(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> LiveDeploymentOut:
    deployment = await db.get(LiveDeployment, uuid.UUID(deployment_id))
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    if deployment.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your deployment")

    deployment.status = "stopped"
    deployment.stopped_at = datetime.now(timezone.utc)
    await write_audit_log(db, user_id=user.id, action="LIVE_TRADING_STOPPED", object_type="live_deployment", object_id=str(deployment.id))
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.post("/deployments/{deployment_id}/evaluate", response_model=EvaluationOut)
async def evaluate_live_deployment_now(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> EvaluationOut:
    deployment = await db.get(LiveDeployment, uuid.UUID(deployment_id))
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    if deployment.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your deployment")

    outcome = await evaluate_live_deployment(db, deployment)
    return EvaluationOut(action=outcome.action, signal=outcome.signal, price=outcome.price, reason=outcome.reason)


@router.get("/orders", response_model=list[LiveOrderOut])
async def list_live_orders(deployment_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[LiveOrderOut]:
    result = await db.execute(select(LiveOrder).where(LiveOrder.deployment_id == uuid.UUID(deployment_id)).order_by(LiveOrder.created_at.desc()))
    return [
        LiveOrderOut(
            id=str(o.id), client_order_id=o.client_order_id, broker_order_id=o.broker_order_id, side=o.side,
            quantity=o.quantity, status=o.status, reason=o.reason, created_at=o.created_at, confirmed_at=o.confirmed_at,
        )
        for o in result.scalars().all()
    ]


@router.get("/reconcile", response_model=ReconciliationOut)
async def reconcile(broker_account_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> ReconciliationOut:
    broker_account = await db.get(BrokerAccount, uuid.UUID(broker_account_id))
    if broker_account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broker account not found")
    if broker_account.user_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your broker account")

    report = await reconcile_positions(db, broker_account)
    return ReconciliationOut(
        clean=report.clean, matched=report.matched, local_only=report.local_only,
        broker_only=report.broker_only, quantity_mismatches=report.quantity_mismatches,
    )


@router.get("/kill-switch", response_model=KillSwitchOut)
async def get_kill_switch_status(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> KillSwitchOut:
    switch = await kill_switch_service.get_kill_switch(db)
    await db.commit()
    return KillSwitchOut(active=switch.active, activated_at=switch.activated_at, reason=switch.reason)


@router.post("/kill-switch/activate", response_model=KillSwitchOut)
async def activate_kill_switch(
    payload: KillSwitchActivate, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator"))
) -> KillSwitchOut:
    switch = await kill_switch_service.activate(db, user.id, payload.reason)

    stopped = await db.execute(select(LiveDeployment).where(LiveDeployment.status == "active"))
    for deployment in stopped.scalars().all():
        deployment.status = "stopped"
        deployment.stopped_at = datetime.now(timezone.utc)

    await write_audit_log(db, user_id=user.id, action="KILL_SWITCH_ACTIVATED", new_value={"reason": payload.reason})
    await db.commit()
    return KillSwitchOut(active=switch.active, activated_at=switch.activated_at, reason=switch.reason)


@router.post("/kill-switch/deactivate", response_model=KillSwitchOut)
async def deactivate_kill_switch(db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator"))) -> KillSwitchOut:
    switch = await kill_switch_service.deactivate(db)
    await write_audit_log(db, user_id=user.id, action="KILL_SWITCH_DEACTIVATED")
    await db.commit()
    return KillSwitchOut(active=switch.active, activated_at=switch.activated_at, reason=switch.reason)
