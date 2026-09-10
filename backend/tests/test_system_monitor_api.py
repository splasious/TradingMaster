import json
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_health_reports_kite_ticker_status_without_gating_overall(client: AsyncClient):
    resp = await client.get("/api/v1/system/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "kite_ticker" in body["components"]
    # Non-core, like market_data_delta -- never blocks "healthy" on its own.
    assert body["components"]["kite_ticker"] in ("connecting", "connected") or body["components"]["kite_ticker"].startswith("error:")


async def test_health_kite_diagnostic_shows_zero_when_nothing_connected(client: AsyncClient):
    resp = await client.get("/api/v1/system/health")
    assert resp.status_code == 200
    assert resp.json()["kite_diagnostic"]["connected_accounts"] == 0


async def test_health_kite_diagnostic_flags_missing_access_token(client: AsyncClient, db_session: AsyncSession):
    """Reproduces a "shows Connected in Settings but the ticker still says
    disconnected" report: a CONNECTED BrokerConnection row whose stored
    credential is missing access_token -- diagnose_zerodha_connection must
    surface exactly this, not just "0 connected accounts", or the report
    is unactionable without DB access."""
    from app.models.user import Role, User, UserRole

    role = Role(id=uuid.uuid4(), name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"diag_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Diag Test")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()
    broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Diag Test", environment="paper")
    db_session.add(account)
    await db_session.flush()
    creds = {"api_key": "kitekey", "api_secret": "kitesecret"}  # no access_token
    db_session.add(BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps(creds))))
    db_session.add(BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTED.value))
    await db_session.commit()

    resp = await client.get("/api/v1/system/health")
    assert resp.status_code == 200
    diag = resp.json()["kite_diagnostic"]
    assert diag["connected_accounts"] == 1
    assert diag["has_credential"] is True
    assert diag["has_api_key"] is True
    assert diag["has_access_token"] is False


async def test_system_monitor_returns_real_metrics(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/api/v1/system/monitor", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert 0.0 <= body["infrastructure"]["cpu_percent"] <= 100.0
    assert body["infrastructure"]["memory_total_mb"] > 0
    assert body["application"]["uptime_seconds"] >= 0
    assert body["trading"]["active_paper_deployments"] == 0
    assert body["trading"]["active_live_deployments"] == 0


async def test_system_monitor_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/system/monitor")
    assert resp.status_code == 401
