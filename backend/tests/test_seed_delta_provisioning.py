import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.user import Role, User, UserRole
from app.seed import _provision_delta_account


async def _admin(db_session: AsyncSession) -> User:
    role = Role(name="administrator", description="Administrator")
    db_session.add(role)
    await db_session.flush()
    user = User(email="seedtest@tradingmaster.internal", hashed_password=hash_password("x"), full_name="Seed Admin")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    db_session.add(Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True))
    await db_session.commit()
    return user


def _patch_delta_auth_ok(monkeypatch):
    async def fake_request(client_self, method, url, headers=None, content=None, **kwargs):
        payload = {"success": True, "result": [{"asset_symbol": "USD", "balance": "1000", "available_balance": "900"}]}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


def _patch_delta_ip_blocked(monkeypatch):
    async def fake_request(client_self, method, url, headers=None, content=None, **kwargs):
        payload = {"success": False, "error": {"code": "ip_not_whitelisted_for_api_key", "context": {"client_ip": "1.2.3.4"}}}
        return httpx.Response(401, content=json.dumps(payload).encode(), request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def test_provisioning_stores_credentials_encrypted_never_plaintext(db_session: AsyncSession, monkeypatch):
    admin = await _admin(db_session)
    _patch_delta_auth_ok(monkeypatch)

    settings = get_settings()
    monkeypatch.setattr(settings, "delta_api_key", "test-key-123")
    monkeypatch.setattr(settings, "delta_api_secret", "test-secret-456")

    await _provision_delta_account(db_session, admin)
    await db_session.commit()

    credential = (await db_session.execute(select(BrokerCredential))).scalar_one()
    assert "test-key-123" not in credential.encrypted_payload
    assert "test-secret-456" not in credential.encrypted_payload

    connection = (await db_session.execute(select(BrokerConnection))).scalar_one()
    assert connection.status == ConnectionStatus.CONNECTED.value


async def test_provisioning_records_honest_error_on_auth_failure(db_session: AsyncSession, monkeypatch):
    admin = await _admin(db_session)
    settings = get_settings()
    monkeypatch.setattr(settings, "delta_api_key", "test-key-123")
    monkeypatch.setattr(settings, "delta_api_secret", "test-secret-456")
    _patch_delta_ip_blocked(monkeypatch)

    await _provision_delta_account(db_session, admin)
    await db_session.commit()

    connection = (await db_session.execute(select(BrokerConnection))).scalar_one()
    assert connection.status == ConnectionStatus.ERROR.value
    assert "not whitelisted" in connection.last_error


async def test_provisioning_is_idempotent(db_session: AsyncSession, monkeypatch):
    admin = await _admin(db_session)
    settings = get_settings()
    monkeypatch.setattr(settings, "delta_api_key", "test-key-123")
    monkeypatch.setattr(settings, "delta_api_secret", "test-secret-456")
    _patch_delta_auth_ok(monkeypatch)

    await _provision_delta_account(db_session, admin)
    await db_session.commit()
    await _provision_delta_account(db_session, admin)
    await db_session.commit()

    accounts = (await db_session.execute(select(BrokerAccount))).scalars().all()
    assert len(accounts) == 1
