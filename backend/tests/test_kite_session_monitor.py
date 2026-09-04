import json
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.services.broker.kite_session_monitor import KiteSessionMonitorScheduler

_original_request = httpx.AsyncClient.request
_KITE_HOST = "api.kite.trade"


def _patch_kite_profile(monkeypatch, *, status_code: int = 200, payload: dict | None = None):
    payload = payload or {"status": "success", "data": {"user_id": "AB1234"}}
    calls: list[str] = []

    async def fake_request(client_self, method, url, headers=None, params=None, data=None, **kwargs):
        if httpx.URL(str(url)).host != _KITE_HOST:
            return await _original_request(client_self, method, url, headers=headers, params=params, data=data, **kwargs)
        calls.append(str(url))
        return httpx.Response(status_code, json=payload, request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    return calls


async def _kite_account(db: AsyncSession, *, status: str) -> BrokerAccount:
    broker = (await db.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one()
    account = BrokerAccount(user_id=uuid.uuid4(), broker_id=broker.id, account_label="Primary", environment="live")
    db.add(account)
    await db.flush()
    db.add(
        BrokerCredential(
            broker_account_id=account.id,
            encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s", "access_token": "tok"})),
        )
    )
    db.add(BrokerConnection(broker_account_id=account.id, status=status, last_heartbeat_at=datetime.now(timezone.utc)))
    await db.commit()
    return account


async def test_check_flips_connected_account_to_error_when_session_expired(db_session: AsyncSession, seeded_admin, monkeypatch):
    account = await _kite_account(db_session, status=ConnectionStatus.CONNECTED.value)
    _patch_kite_profile(
        monkeypatch, status_code=403,
        payload={"status": "error", "error_type": "TokenException", "message": "Incorrect `api_key` or `access_token`."},
    )

    checked = await KiteSessionMonitorScheduler().check(db_session)
    assert checked == 1

    await db_session.refresh(account, attribute_names=["connection"])
    assert account.connection.status == ConnectionStatus.ERROR.value
    assert "Incorrect" in account.connection.last_error


async def test_check_leaves_healthy_session_connected(db_session: AsyncSession, seeded_admin, monkeypatch):
    account = await _kite_account(db_session, status=ConnectionStatus.CONNECTED.value)
    calls = _patch_kite_profile(monkeypatch)

    checked = await KiteSessionMonitorScheduler().check(db_session)
    assert checked == 1
    assert len(calls) == 1

    await db_session.refresh(account, attribute_names=["connection"])
    assert account.connection.status == ConnectionStatus.CONNECTED.value
    assert account.connection.last_error is None


async def test_check_skips_accounts_not_currently_connected(db_session: AsyncSession, seeded_admin, monkeypatch):
    await _kite_account(db_session, status=ConnectionStatus.DISCONNECTED.value)
    calls = _patch_kite_profile(monkeypatch)

    checked = await KiteSessionMonitorScheduler().check(db_session)
    assert checked == 0
    assert calls == []


async def test_check_skips_non_kite_brokers(db_session: AsyncSession, seeded_admin, monkeypatch):
    broker = (await db_session.execute(select(Broker).where(Broker.code == "delta_exchange"))).scalar_one()
    account = BrokerAccount(user_id=uuid.uuid4(), broker_id=broker.id, account_label="Delta", environment="live")
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s"})))
    )
    db_session.add(BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTED.value))
    await db_session.commit()

    calls = _patch_kite_profile(monkeypatch)
    checked = await KiteSessionMonitorScheduler().check(db_session)
    assert checked == 0
    assert calls == []
