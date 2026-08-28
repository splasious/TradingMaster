import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_user, require_role
from app.core.encryption import encrypt_payload
from app.db.session import get_db
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.user import User
from app.schemas.broker import BrokerAccountCreate, BrokerAccountOut, BrokerOut
from app.services.audit import write_audit_log
from app.services.broker.registry import get_broker_adapter, is_real_adapter

router = APIRouter()


def _broker_out(broker: Broker) -> BrokerOut:
    return BrokerOut(
        id=str(broker.id), code=broker.code, name=broker.name, is_enabled=broker.is_enabled,
        is_real_adapter=is_real_adapter(broker.code),
    )


def _account_out(account: BrokerAccount) -> BrokerAccountOut:
    return BrokerAccountOut(
        id=str(account.id),
        broker=_broker_out(account.broker),
        account_label=account.account_label,
        environment=account.environment,
        is_active=account.is_active,
        connection_status=account.connection.status if account.connection else ConnectionStatus.DISCONNECTED.value,
    )


@router.get("", response_model=list[BrokerOut])
async def list_brokers(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[BrokerOut]:
    result = await db.execute(select(Broker).order_by(Broker.name))
    return [_broker_out(b) for b in result.scalars().all()]


@router.get("/accounts", response_model=list[BrokerAccountOut])
async def list_broker_accounts(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[BrokerAccountOut]:
    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.connection))
        .where(BrokerAccount.user_id == user.id)
        .order_by(BrokerAccount.created_at)
    )
    return [_account_out(a) for a in result.scalars().all()]


@router.post("/accounts", response_model=BrokerAccountOut, status_code=status.HTTP_201_CREATED)
async def connect_broker_account(
    payload: BrokerAccountCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader")),
) -> BrokerAccountOut:
    broker_result = await db.execute(select(Broker).where(Broker.code == payload.broker_code))
    broker = broker_result.scalar_one_or_none()
    if broker is None or not broker.is_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or disabled broker")

    account = BrokerAccount(
        user_id=user.id, broker_id=broker.id, account_label=payload.account_label, environment=payload.environment
    )
    db.add(account)
    await db.flush()

    db.add(
        BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps(payload.credentials)))
    )
    connection = BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTING.value)
    db.add(connection)
    await db.flush()

    adapter = get_broker_adapter(broker.code)
    try:
        authenticated = await adapter.authenticate(payload.credentials)
        if authenticated:
            await adapter.connect()
            connection.status = ConnectionStatus.CONNECTED.value
            connection.last_heartbeat_at = datetime.now(timezone.utc)
            connection.last_error = None
        else:
            connection.status = ConnectionStatus.ERROR.value
            connection.last_error = "Authentication rejected by broker"
    except Exception as exc:  # adapter failures must surface as ERROR, never crash the request
        connection.status = ConnectionStatus.ERROR.value
        connection.last_error = str(exc)

    await write_audit_log(
        db,
        user_id=user.id,
        action="BROKER_CONNECTED",
        object_type="broker_account",
        object_id=str(account.id),
        new_value={"broker_code": broker.code, "environment": payload.environment, "status": connection.status},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.connection))
        .where(BrokerAccount.id == account.id)
    )
    return _account_out(result.scalar_one())


@router.post("/accounts/{account_id}/disconnect", response_model=BrokerAccountOut)
async def disconnect_broker_account(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader")),
) -> BrokerAccountOut:
    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.connection))
        .where(BrokerAccount.id == uuid.UUID(account_id), BrokerAccount.user_id == user.id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broker account not found")

    adapter = get_broker_adapter(account.broker.code)
    await adapter.disconnect()

    if account.connection is None:
        account.connection = BrokerConnection(broker_account_id=account.id)
    account.connection.status = ConnectionStatus.DISCONNECTED.value
    account.connection.last_heartbeat_at = None

    await write_audit_log(
        db,
        user_id=user.id,
        action="BROKER_DISCONNECTED",
        object_type="broker_account",
        object_id=str(account.id),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(account, attribute_names=["connection"])
    return _account_out(account)
