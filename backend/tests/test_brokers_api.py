import hashlib
import json
import sys
import types
import uuid

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

_original_request = httpx.AsyncClient.request
_DELTA_HOST = "api.india.delta.exchange"
_KITE_HOST = "api.kite.trade"
_HDFC_HOST = "developer.hdfcsec.com"


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


def _patch_hdfc(monkeypatch, *, token_status: int = 200, token_payload: dict | None = None):
    token_payload = token_payload or {"data": {"access_token": "sess_tok_xyz"}}

    async def fake_request(client_self, method, url, headers=None, params=None, json=None, **kwargs):
        if httpx.URL(str(url)).host != _HDFC_HOST:
            return await _original_request(client_self, method, url, headers=headers, params=params, json=json, **kwargs)
        if str(url).endswith("/access-token"):
            return httpx.Response(token_status, json=token_payload, request=httpx.Request(method, str(url)))
        return httpx.Response(200, json={"client_id": "C123"}, request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


def _patch_kotak_neo_sdk(monkeypatch):
    class _FakeNeoAPI:
        def __init__(self, environment=None, access_token=None, neo_fin_key=None, consumer_key=None):
            pass

        def totp_login(self, mobile_number=None, ucc=None, totp=None):
            return {"data": {"token": "view_tok"}}

        def totp_validate(self, mpin=None):
            return {"data": {"token": "trade_tok"}}

    monkeypatch.setitem(sys.modules, "neo_api_client", types.SimpleNamespace(NeoAPI=_FakeNeoAPI))
    monkeypatch.setitem(sys.modules, "pyotp", types.SimpleNamespace(TOTP=lambda secret: types.SimpleNamespace(now=lambda: "123456")))


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


async def test_kite_callback_reconnect_uses_fresh_request_token_not_stale_access_token(client: AsyncClient, seeded_admin: dict, monkeypatch):
    """Regression test for a real production bug: after the first
    successful login, the stored credential permanently carries that day's
    access_token. A second callback (the very next day's mandatory
    re-login, same account) merges a NEW request_token in alongside that
    now-stale access_token -- authenticate() must exchange the fresh
    request_token via /session/token, not silently re-validate the dead
    access_token via /user/profile (which is what actually happened before
    this was fixed, permanently locking every account out after its first
    successful connection)."""
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})

    call_log: list[str] = []

    def make_fake(session_token_value: str):
        async def fake_request(client_self, method, url, headers=None, params=None, data=None, **kwargs):
            if httpx.URL(str(url)).host != _KITE_HOST:
                return await _original_request(client_self, method, url, headers=headers, params=params, data=data, **kwargs)
            path = httpx.URL(str(url)).path
            call_log.append(path)
            if path == "/session/token":
                assert data["checksum"] == hashlib.sha256(f"{data['api_key']}{data['request_token']}kitesecret".encode()).hexdigest()
                return httpx.Response(200, json={"status": "success", "data": {"access_token": session_token_value, "user_id": "AB1234"}}, request=httpx.Request(method, str(url)))
            if path == "/user/profile":
                # Simulates the OLD access_token being genuinely expired (Kite's
                # daily session expiry) -- if the code wrongly tries this branch
                # instead of exchanging the fresh request_token, it must fail.
                return httpx.Response(403, json={"status": "error", "error_type": "TokenException", "message": "Incorrect `api_key` or `access_token`."}, request=httpx.Request(method, str(url)))
            raise AssertionError(f"Unexpected Kite call: {method} {path}")
        return fake_request

    # First login -- account has no access_token yet, so this legitimately
    # goes through /session/token regardless of the bug.
    monkeypatch.setattr(httpx.AsyncClient, "request", make_fake("day1_access_token"))
    first = await client.post(f"/api/v1/brokers/accounts/{account['id']}/kite/callback", json={"request_token": "req_tok_day1"}, headers=headers)
    assert first.json()["connection_status"] == "connected"

    # Second login (next day) -- the stored credential now carries
    # "day1_access_token" (stale/expired) AND this new request_token. This
    # is exactly the scenario the bug broke.
    call_log.clear()
    monkeypatch.setattr(httpx.AsyncClient, "request", make_fake("day2_access_token"))
    second = await client.post(f"/api/v1/brokers/accounts/{account['id']}/kite/callback", json={"request_token": "req_tok_day2"}, headers=headers)
    assert second.json()["connection_status"] == "connected"
    assert "/session/token" in call_log
    assert "/user/profile" not in call_log


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


async def test_connect_kotak_neo_account_authenticates_immediately(client: AsyncClient, seeded_admin: dict, monkeypatch):
    """Unlike Zerodha/HDFC's OAuth-redirect flow, Kotak Neo's TOTP+MPIN
    auth completes in a single authenticate() call (see registry.py's
    _INTERACTIVE_AUTH_BROKERS) -- connecting an account should reach
    "connected" immediately, the same as Delta."""
    _patch_kotak_neo_sdk(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    account = await _connect(client, headers, "kotak_neo", {
        "consumer_key": "ck", "mobile_number": "+919999999999", "ucc": "ABC123",
        "totp_secret": "JBSWY3DPEHPK3PXP", "mpin": "123456",
    })
    assert account["connection_status"] == "connected"


async def test_connect_hdfc_account_is_disconnected_pending_login(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    account = await _connect(client, headers, "hdfc_securities", {"api_key": "hdfckey", "api_secret": "hdfcsecret"})
    assert account["connection_status"] == "disconnected"


async def test_hdfc_login_url_reflects_stored_api_key(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    account = await _connect(client, headers, "hdfc_securities", {"api_key": "hdfckey", "api_secret": "hdfcsecret"})
    resp = await client.get(f"/api/v1/brokers/accounts/{account['id']}/hdfc/login-url", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["login_url"] == "https://developer.hdfcsec.com/oapi/v1/login?api_key=hdfckey"


async def test_hdfc_callback_completes_connection(client: AsyncClient, seeded_admin: dict, monkeypatch):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "hdfc_securities", {"api_key": "hdfckey", "api_secret": "hdfcsecret"})

    _patch_hdfc(monkeypatch)
    resp = await client.post(f"/api/v1/brokers/accounts/{account['id']}/hdfc/callback", json={"auth_code": "auth_abc"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["connection_status"] == "connected"


async def test_hdfc_callback_surfaces_broker_error(client: AsyncClient, seeded_admin: dict, monkeypatch):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "hdfc_securities", {"api_key": "hdfckey", "api_secret": "hdfcsecret"})

    _patch_hdfc(monkeypatch, token_status=400, token_payload={"status": "error", "message": "Invalid auth_code"})
    resp = await client.post(f"/api/v1/brokers/accounts/{account['id']}/hdfc/callback", json={"auth_code": "bad"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["connection_status"] == "error"


async def test_hdfc_endpoints_reject_non_hdfc_account(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_delta_ok(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "delta_exchange", {"api_key": "k", "api_secret": "s"})

    resp = await client.get(f"/api/v1/brokers/accounts/{account['id']}/hdfc/login-url", headers=headers)
    assert resp.status_code == 400


async def test_update_broker_account_credentials_reuses_same_row(client: AsyncClient, seeded_admin: dict):
    """Fixing a wrong api_secret must update the existing account, not
    require a brand-new "Connect Broker" row -- the bug this endpoint
    exists to fix."""
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "zerodha_kite", {"api_key": "wrongkey", "api_secret": "wrongsecret"})
    account_id = account["id"]

    resp = await client.patch(
        f"/api/v1/brokers/accounts/{account_id}",
        json={"credentials": {"api_key": "kitekey", "api_secret": "kitesecret"}},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == account_id  # same row, not a new one
    assert body["connection_status"] == "disconnected"  # Kite still needs the interactive login step

    # The updated api_key is what the login URL now reflects -- proof the
    # credential really changed, not just the label.
    login_resp = await client.get(f"/api/v1/brokers/accounts/{account_id}/kite/login-url", headers=headers)
    assert login_resp.json()["login_url"] == "https://kite.zerodha.com/connect/login?v=3&api_key=kitekey"


async def test_update_broker_account_label_only_leaves_credentials_untouched(client: AsyncClient, seeded_admin: dict, monkeypatch):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    _patch_delta_ok(monkeypatch)
    account = await _connect(client, headers, "delta_exchange", {"api_key": "k", "api_secret": "s"})

    resp = await client.patch(
        f"/api/v1/brokers/accounts/{account['id']}", json={"account_label": "Renamed"}, headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_label"] == "Renamed"
    assert body["connection_status"] == "connected"  # untouched, not reset by a label-only edit


async def test_update_broker_account_rejects_other_users_account(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})

    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    other = User(email="other_broker@tradingmaster.internal", hashed_password=hash_password("OtherPass123!"), full_name="Other")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    other_token = await _login(client, "other_broker@tradingmaster.internal", "OtherPass123!")
    resp = await client.patch(
        f"/api/v1/brokers/accounts/{account['id']}",
        json={"account_label": "Hijacked"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 404


async def test_delete_broker_account_removes_it(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})

    resp = await client.delete(f"/api/v1/brokers/accounts/{account['id']}", headers=headers)
    assert resp.status_code == 204

    list_resp = await client.get("/api/v1/brokers/accounts", headers=headers)
    assert account["id"] not in [a["id"] for a in list_resp.json()]


async def test_delete_broker_account_blocked_by_active_live_deployment(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from app.models.instrument import Instrument
    from app.models.live_trading import LiveDeployment
    from app.models.strategy import Strategy, StrategyVersion

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    account = await _connect(client, headers, "zerodha_kite", {"api_key": "kitekey", "api_secret": "kitesecret"})

    from sqlalchemy import select

    from app.core.security import hash_password  # noqa: F401 (keeps import style consistent with other tests)
    from app.models.user import User

    admin_user = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    instrument = Instrument(exchange="NSE", symbol="TESTBROKERDEL", name="Test", instrument_type="equity", data_source="zerodha_kite", external_ref="TESTBROKERDEL")
    db_session.add(instrument)
    await db_session.flush()
    strategy = Strategy(name="Del Test Strategy", owner_id=admin_user.id, code_type="python")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[str(instrument.id)], parameters={},
        python_code='def generate_signal(c,p):\n    return "HOLD"', position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()
    deployment = LiveDeployment(
        owner_id=admin_user.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        broker_account_id=uuid.UUID(account["id"]), timeframe="1d", status="active",
    )
    db_session.add(deployment)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/brokers/accounts/{account['id']}", headers=headers)
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
