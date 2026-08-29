import hashlib
import json

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

_original_request = httpx.AsyncClient.request
_DELTA_HOST = "api.india.delta.exchange"
_KITE_HOST = "api.kite.trade"


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _patch_delta_ok(monkeypatch):
    """Only fakes requests bound for Delta's API host -- the FastAPI test
    `client` fixture is also an httpx.AsyncClient (over ASGITransport), so
    an unconditional patch would hijack the test's own login/API calls."""

    async def fake_request(client_self, method, url, headers=None, content=None, **kwargs):
        if httpx.URL(str(url)).host != _DELTA_HOST:
            return await _original_request(client_self, method, url, headers=headers, content=content, **kwargs)
        payload = {"success": True, "result": [{"asset_symbol": "USD", "balance": "1", "available_balance": "1"}]}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


def _patch_kite(monkeypatch, *, session_status: int = 200, session_payload: dict | None = None):
    session_payload = session_payload or {"status": "success", "data": {"access_token": "sess_tok_xyz", "user_id": "AB1234"}}

    async def fake_request(client_self, method, url, headers=None, params=None, data=None, **kwargs):
        if httpx.URL(str(url)).host != _KITE_HOST:
            return await _original_request(client_self, method, url, headers=headers, params=params, data=data, **kwargs)
        if str(url).endswith("/session/token"):
            assert data["checksum"] == hashlib.sha256(f"{data['api_key']}{data['request_token']}kitesecret".encode()).hexdigest()
            return httpx.Response(session_status, json=session_payload, request=httpx.Request(method, str(url)))
        return httpx.Response(200, json={"status": "success", "data": {"user_id": "AB1234"}}, request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def _connect(client: AsyncClient, headers: dict, broker_code: str, credentials: dict) -> dict:
    resp = await client.post(
        "/api/v1/brokers/accounts",
        json={"broker_code": broker_code, "account_label": "Primary", "environment": "live", "credentials": credentials},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()


async def test_connect_delta_account_authenticates_immediately(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_delta_ok(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    account = await _connect(client, headers, "delta_exchange", {"api_key": "k", "api_secret": "s"})
    assert account["connection_status"] == "connected"


async def test_connect_zerodha_account_is_disconnected_pending_login(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})
    assert account["connection_status"] == "disconnected"


async def test_kite_login_url_reflects_stored_api_key(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})
    resp = await client.get(f"/api/v1/brokers/accounts/{account['id']}/kite/login-url", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["login_url"] == "https://kite.zerodha.com/connect/login?v=3&api_key=kitekey"


async def test_kite_callback_completes_connection(client: AsyncClient, seeded_admin: dict, monkeypatch):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})

    _patch_kite(monkeypatch)
    resp = await client.post(f"/api/v1/brokers/accounts/{account['id']}/kite/callback", json={"request_token": "req_tok_abc"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["connection_status"] == "connected"


async def test_kite_callback_surfaces_broker_error(client: AsyncClient, seeded_admin: dict, monkeypatch):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})

    _patch_kite(monkeypatch, session_status=403, session_payload={"status": "error", "error_type": "TokenException", "message": "Invalid request token"})
    resp = await client.post(f"/api/v1/brokers/accounts/{account['id']}/kite/callback", json={"request_token": "bad_token"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["connection_status"] == "error"


async def test_kite_endpoints_reject_non_kite_account(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_delta_ok(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "delta_exchange", {"api_key": "k", "api_secret": "s"})

    resp = await client.get(f"/api/v1/brokers/accounts/{account['id']}/kite/login-url", headers=headers)
    assert resp.status_code == 400


async def test_kite_endpoints_require_trader_or_admin_role(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    role = (await db_session.execute(select(Role).where(Role.name == "viewer"))).scalar_one()
    viewer = User(email="viewer_broker@tradingmaster.internal", hashed_password=hash_password("ViewerPass123!"), full_name="Viewer")
    viewer.user_roles = [UserRole(role=role)]
    db_session.add(viewer)
    await db_session.commit()

    viewer_token = await _login(client, "viewer_broker@tradingmaster.internal", "ViewerPass123!")
    resp = await client.get(
        "/api/v1/brokers/accounts/00000000-0000-0000-0000-000000000000/kite/login-url",
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert resp.status_code == 403
