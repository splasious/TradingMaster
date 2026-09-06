import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.services.backfill_platform.kite_auth import get_authenticated_kite_broker
from app.services.backfill_platform.status import zerodha_status
from app.services.broker.zerodha_broker import KiteAPIError

_original_request = httpx.AsyncClient.request
_KITE_HOST = "api.kite.trade"


def _patch_kite_profile(monkeypatch, *, status_code: int = 200, payload: dict | None = None):
    payload = payload or {"status": "success", "data": {"user_id": "AB1234"}}

    async def fake_request(client_self, method, url, headers=None, params=None, data=None, **kwargs):
        if httpx.URL(str(url)).host != _KITE_HOST:
            return await _original_request(client_self, method, url, headers=headers, params=params, data=data, **kwargs)
        return httpx.Response(status_code, json=payload, request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def _kite_account(
    db: AsyncSession, *, user_id, status: str, access_token: str, created_at: datetime,
) -> BrokerAccount:
    broker = (await db.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one()
    account = BrokerAccount(
        user_id=user_id, broker_id=broker.id, account_label="Zerodha", environment="live", created_at=created_at,
    )
    db.add(account)
    await db.flush()
    db.add(
        BrokerCredential(
            broker_account_id=account.id,
            encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s", "access_token": access_token})),
        )
    )
    db.add(BrokerConnection(broker_account_id=account.id, status=status))
    await db.commit()
    return account


async def test_prefers_connected_account_over_older_broken_one(db_session: AsyncSession, seeded_admin, monkeypatch):
    """Reproduces the real production bug: a user reconnects Zerodha via
    'Connect Broker' (a new BrokerAccount row) instead of 'Login with
    Zerodha' on the existing one after the daily session expired -- two
    rows now exist for the same user, the old one permanently ERROR.
    Without ordering, plain .first() picked whichever row Postgres
    happened to scan first (the stale one in practice), so every backfill
    kept failing with a dead token even after the user genuinely
    reconnected."""
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await _kite_account(db_session, user_id=user_id, status=ConnectionStatus.ERROR.value, access_token="dead_token", created_at=now - timedelta(days=2))
    await _kite_account(db_session, user_id=user_id, status=ConnectionStatus.CONNECTED.value, access_token="fresh_token", created_at=now)

    _patch_kite_profile(monkeypatch)
    broker = await get_authenticated_kite_broker(db_session, user_id)
    assert broker.access_token == "fresh_token"


async def test_falls_back_to_most_recent_when_none_connected(db_session: AsyncSession, seeded_admin, monkeypatch):
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await _kite_account(db_session, user_id=user_id, status=ConnectionStatus.ERROR.value, access_token="older_token", created_at=now - timedelta(days=2))
    await _kite_account(db_session, user_id=user_id, status=ConnectionStatus.DISCONNECTED.value, access_token="newer_token", created_at=now)

    _patch_kite_profile(monkeypatch)
    broker = await get_authenticated_kite_broker(db_session, user_id)
    assert broker.access_token == "newer_token"


async def test_raises_when_no_account_connected(db_session: AsyncSession, seeded_admin):
    with pytest.raises(KiteAPIError, match="No Zerodha Kite account connected"):
        await get_authenticated_kite_broker(db_session, uuid.uuid4())


async def test_status_reflects_connected_account_not_an_older_broken_one(db_session: AsyncSession, seeded_admin):
    """Same bug, different call site: zerodha_status() had the identical
    unordered .first() as get_authenticated_kite_broker, so the "Zerodha
    Kite" status card on the Data Backfill Platform kept showing an old
    account's "Disconnected"/TokenException from days earlier even while
    the real, currently-connected account (reconnected minutes before) was
    healthy -- confirmed live, right after the get_authenticated_kite_broker
    fix already shipped, so this was a separate lingering instance of the
    same class of bug."""
    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    await _kite_account(db_session, user_id=user_id, status=ConnectionStatus.ERROR.value, access_token="dead_token", created_at=now - timedelta(days=2))
    await _kite_account(db_session, user_id=user_id, status=ConnectionStatus.CONNECTED.value, access_token="fresh_token", created_at=now)

    result = await zerodha_status(db_session, user_id)
    assert result.connected is True
    assert result.detail == "Connected"
