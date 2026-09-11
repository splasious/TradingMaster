import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_user, require_role
from app.core.encryption import decrypt_payload, encrypt_payload
from app.db.session import get_db
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.live_trading import LiveDeployment
from app.models.user import User
from app.schemas.broker import (
    BrokerAccountCreate, BrokerAccountOut, BrokerAccountUpdate, BrokerBalanceOut, BrokerOut,
    HDFCCallbackIn, HDFCLoginUrlOut, KiteCallbackIn, KiteLoginUrlOut,
)
from app.services.audit import write_audit_log
from app.services.broker.hdfc_securities_broker import HDFCSecuritiesBroker
from app.services.broker.registry import get_broker_adapter, is_real_adapter, requires_interactive_auth
from app.services.broker.zerodha_broker import ZerodhaKiteBroker

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
        connection_last_error=account.connection.last_error if account.connection else None,
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


async def _authenticate_and_set_status(connection: BrokerConnection, broker_code: str, credentials: dict) -> None:
    """Shared by connect (new account) and update (credentials changed on
    an existing one) -- same rule either way: an adapter needing an
    interactive browser login (Kite) can't finish here, everything else
    authenticates in one shot."""
    if requires_interactive_auth(broker_code):
        # Can't finish authenticating yet -- api_key/api_secret are stored,
        # but Kite Connect needs a real browser login before a
        # request_token exists to exchange for an access_token. Left
        # disconnected (not ERROR -- this isn't a failure) until the
        # frontend completes /kite/login-url -> /kite/callback.
        connection.status = ConnectionStatus.DISCONNECTED.value
        connection.last_error = "Awaiting Zerodha login -- use 'Login with Zerodha' to finish connecting."
        return
    adapter = get_broker_adapter(broker_code)
    try:
        authenticated = await adapter.authenticate(credentials)
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

    await _authenticate_and_set_status(connection, broker.code, payload.credentials)

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


@router.patch("/accounts/{account_id}", response_model=BrokerAccountOut)
async def update_broker_account(
    account_id: str,
    payload: BrokerAccountUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader")),
) -> BrokerAccountOut:
    """Edits an existing account in place -- account_label and/or
    credentials -- instead of forcing a brand-new row every time a wrong
    api_key/api_secret needs correcting (the previous only options were
    "create another account" or "disconnect", neither of which could fix a
    bad credential on the same row)."""
    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.connection), selectinload(BrokerAccount.credential))
        .where(BrokerAccount.id == uuid.UUID(account_id), BrokerAccount.user_id == user.id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broker account not found")

    if payload.account_label is not None:
        account.account_label = payload.account_label

    credentials_changed = bool(payload.credentials)
    if credentials_changed:
        if account.credential is None:
            account.credential = BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps(payload.credentials)))
            db.add(account.credential)
        else:
            account.credential.encrypted_payload = encrypt_payload(json.dumps(payload.credentials))

        # A changed credential invalidates whatever the old connection state
        # meant (e.g. a Kite access_token tied to the OLD api_key/secret
        # pair) -- always re-derive status from scratch, same rule as a
        # fresh connect.
        if account.connection is None:
            account.connection = BrokerConnection(broker_account_id=account.id)
            db.add(account.connection)
        await _authenticate_and_set_status(account.connection, account.broker.code, payload.credentials)

    await write_audit_log(
        db,
        user_id=user.id,
        action="BROKER_ACCOUNT_UPDATED",
        object_type="broker_account",
        object_id=str(account.id),
        new_value={"account_label": account.account_label, "credentials_changed": credentials_changed},
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


@router.get("/accounts/{account_id}/balance", response_model=BrokerBalanceOut)
async def get_broker_account_balance(
    account_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader"))
) -> BrokerBalanceOut:
    """Real balance straight from the broker (adapter.get_balance()) --
    the Portfolio page's only source for real capital figures, never a
    locally-tracked number. Not polled/auto-refreshed anywhere -- called
    on page-mount only, to respect broker rate limits (Zerodha's 429s
    have already caused real problems elsewhere in this codebase)."""
    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.credential))
        .where(BrokerAccount.id == uuid.UUID(account_id), BrokerAccount.user_id == user.id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broker account not found")
    if account.credential is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No credentials stored for this account")

    creds = json.loads(decrypt_payload(account.credential.encrypted_payload))
    adapter = get_broker_adapter(account.broker.code)
    try:
        await adapter.authenticate(creds)
        balance = await adapter.get_balance()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not fetch balance: {exc}") from exc

    return BrokerBalanceOut(
        available_margin=balance.get("available_margin", 0.0), used_margin=balance.get("used_margin", 0.0),
        currency=balance.get("currency", "INR"),
    )


async def _get_owned_interactive_account(db: AsyncSession, account_id: str, user: User, broker_code: str, broker_label: str) -> BrokerAccount:
    """Shared by Kite and HDFC's login-url/callback endpoints -- both are
    interactive-auth brokers (registry.py's _INTERACTIVE_AUTH_BROKERS)
    needing the exact same "is this account really this broker, and does
    it have stored credentials to build a login URL / exchange a token
    from" check before either step can proceed."""
    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.connection), selectinload(BrokerAccount.credential))
        .where(BrokerAccount.id == uuid.UUID(account_id), BrokerAccount.user_id == user.id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broker account not found")
    if account.broker.code != broker_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"This account is not a {broker_label} account")
    if account.credential is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No credentials stored for this account")
    return account


async def _get_owned_kite_account(db: AsyncSession, account_id: str, user: User) -> BrokerAccount:
    return await _get_owned_interactive_account(db, account_id, user, "zerodha_kite", "Zerodha Kite")


async def _get_owned_hdfc_account(db: AsyncSession, account_id: str, user: User) -> BrokerAccount:
    return await _get_owned_interactive_account(db, account_id, user, "hdfc_securities", "HDFC Securities")


@router.get("/accounts/{account_id}/kite/login-url", response_model=KiteLoginUrlOut)
async def kite_login_url(
    account_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader"))
) -> KiteLoginUrlOut:
    account = await _get_owned_kite_account(db, account_id, user)
    creds = json.loads(decrypt_payload(account.credential.encrypted_payload))
    api_key = creds.get("api_key")
    if not api_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No api_key stored for this account")
    return KiteLoginUrlOut(login_url=ZerodhaKiteBroker.build_login_url(api_key))


@router.post("/accounts/{account_id}/kite/callback", response_model=BrokerAccountOut)
async def kite_callback(
    account_id: str,
    payload: KiteCallbackIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader")),
) -> BrokerAccountOut:
    """Completes the interactive Zerodha login: exchanges the one-time
    request_token Kite handed back after the user logged in for a session
    access_token (a real POST /session/token call), then re-encrypts the
    credentials to include it -- never stored as plaintext, same as every
    other credential in this codebase."""
    account = await _get_owned_kite_account(db, account_id, user)
    creds = json.loads(decrypt_payload(account.credential.encrypted_payload))

    adapter: ZerodhaKiteBroker = get_broker_adapter("zerodha_kite")  # type: ignore[assignment]
    try:
        await adapter.authenticate({**creds, "request_token": payload.request_token})
        await adapter.connect()
        creds["access_token"] = adapter.access_token
        account.credential.encrypted_payload = encrypt_payload(json.dumps(creds))
        if account.connection is None:
            account.connection = BrokerConnection(broker_account_id=account.id)
        account.connection.status = ConnectionStatus.CONNECTED.value
        account.connection.last_heartbeat_at = datetime.now(timezone.utc)
        account.connection.last_error = None
    except Exception as exc:
        if account.connection is None:
            account.connection = BrokerConnection(broker_account_id=account.id)
        account.connection.status = ConnectionStatus.ERROR.value
        account.connection.last_error = str(exc)

    await write_audit_log(
        db, user_id=user.id, action="BROKER_CONNECTED", object_type="broker_account", object_id=str(account.id),
        new_value={"broker_code": "zerodha_kite", "status": account.connection.status},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.connection))
        .where(BrokerAccount.id == account.id)
    )
    return _account_out(result.scalar_one())


@router.get("/accounts/{account_id}/hdfc/login-url", response_model=HDFCLoginUrlOut)
async def hdfc_login_url(
    account_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader"))
) -> HDFCLoginUrlOut:
    account = await _get_owned_hdfc_account(db, account_id, user)
    creds = json.loads(decrypt_payload(account.credential.encrypted_payload))
    api_key = creds.get("api_key")
    if not api_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No api_key stored for this account")
    return HDFCLoginUrlOut(login_url=HDFCSecuritiesBroker.build_login_url(api_key))


@router.post("/accounts/{account_id}/hdfc/callback", response_model=BrokerAccountOut)
async def hdfc_callback(
    account_id: str,
    payload: HDFCCallbackIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader")),
) -> BrokerAccountOut:
    """Completes the interactive HDFC Securities login: exchanges the
    one-time auth_code HDFC hands back after the user logs in for a
    session access_token, then re-encrypts the credentials to include it
    -- mirrors kite_callback exactly (see its docstring); see
    hdfc_securities_broker.py's module docstring for this adapter's
    confirmed-vs-inferred confidence level before relying on it."""
    account = await _get_owned_hdfc_account(db, account_id, user)
    creds = json.loads(decrypt_payload(account.credential.encrypted_payload))

    adapter: HDFCSecuritiesBroker = get_broker_adapter("hdfc_securities")  # type: ignore[assignment]
    try:
        await adapter.authenticate({**creds, "auth_code": payload.auth_code})
        await adapter.connect()
        creds["access_token"] = adapter.access_token
        account.credential.encrypted_payload = encrypt_payload(json.dumps(creds))
        if account.connection is None:
            account.connection = BrokerConnection(broker_account_id=account.id)
        account.connection.status = ConnectionStatus.CONNECTED.value
        account.connection.last_heartbeat_at = datetime.now(timezone.utc)
        account.connection.last_error = None
    except Exception as exc:
        if account.connection is None:
            account.connection = BrokerConnection(broker_account_id=account.id)
        account.connection.status = ConnectionStatus.ERROR.value
        account.connection.last_error = str(exc)

    await write_audit_log(
        db, user_id=user.id, action="BROKER_CONNECTED", object_type="broker_account", object_id=str(account.id),
        new_value={"broker_code": "hdfc_securities", "status": account.connection.status},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    result = await db.execute(
        select(BrokerAccount)
        .options(selectinload(BrokerAccount.broker), selectinload(BrokerAccount.connection))
        .where(BrokerAccount.id == account.id)
    )
    return _account_out(result.scalar_one())


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_broker_account(
    account_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader")),
) -> None:
    """Removes the row entirely -- for cleaning up duplicate/dead accounts
    (e.g. from before update_broker_account existed, when the only way to
    fix or refresh a connection was "Connect Broker" again). Refuses if a
    still-active LiveDeployment depends on it, since that FK cascades on
    delete and would silently take the deployment down with it."""
    result = await db.execute(select(BrokerAccount).where(BrokerAccount.id == uuid.UUID(account_id), BrokerAccount.user_id == user.id))
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Broker account not found")

    active_count = (
        await db.execute(
            select(func.count()).select_from(LiveDeployment)
            .where(LiveDeployment.broker_account_id == account.id, LiveDeployment.status == "active")
        )
    ).scalar_one()
    if active_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{active_count} active live deployment(s) still use this account -- stop them first.",
        )

    await write_audit_log(
        db, user_id=user.id, action="BROKER_ACCOUNT_DELETED", object_type="broker_account", object_id=str(account.id),
        previous_value={"account_label": account.account_label, "environment": account.environment},
        ip_address=request.client.host if request.client else None,
    )
    await db.delete(account)
    await db.commit()
