import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.core.security import hash_password
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.user import Role, User, UserRole


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed_live_ready(db_session: AsyncSession, user, strategy_status="approved"):
    # seeded_admin already creates a "delta_exchange" broker row -- reuse it
    # rather than inserting a duplicate (unique constraint on code).
    broker = (await db_session.execute(select(Broker).where(Broker.code == "delta_exchange"))).scalar_one_or_none()
    if broker is None:
        broker = Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True)
        db_session.add(broker)
        await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="api test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(broker_account_id=broker_account.id, status=ConnectionStatus.CONNECTED.value))
    db_session.add(BrokerCredential(broker_account_id=broker_account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s"}))))

    instrument = Instrument(exchange="DELTA", symbol="APIX", name="API Test Perp", instrument_type="perpetual_future", data_source="delta_exchange", external_ref="APIX")
    db_session.add(instrument)
    await db_session.flush()
    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 100 + i
        db_session.add(OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test"))
    await db_session.commit()
    return broker_account, instrument


_DELTA_HOST = "api.india.delta.exchange"

_original_get = httpx.AsyncClient.get
_original_request = httpx.AsyncClient.request


def _patch_delta_ok(monkeypatch, price=150.0):
    """Only fakes requests actually bound for Delta's API host -- the
    FastAPI test `client` fixture is *also* an httpx.AsyncClient (talking
    to the app over ASGITransport), so an unconditional patch of
    AsyncClient.request would hijack the test's own login/API calls too."""

    async def fake_get(client_self, url, **kwargs):
        if httpx.URL(str(url)).host != _DELTA_HOST:
            return await _original_get(client_self, url, **kwargs)
        payload = {"success": True, "result": {"close": price, "mark_price": price, "product_id": 27}}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    async def fake_request(client_self, method, url, headers=None, content=None, **kwargs):
        if httpx.URL(str(url)).host != _DELTA_HOST:
            return await _original_request(client_self, method, url, headers=headers, content=content, **kwargs)
        path = httpx.URL(str(url)).path
        if path == "/v2/wallet/balances":
            payload = {"success": True, "result": [{"asset_symbol": "USD", "balance": "100000", "available_balance": "100000"}]}
        elif path == "/v2/orders" and method == "POST":
            payload = {"success": True, "result": {"id": 1, "state": "open"}}
        elif path.startswith("/v2/orders/"):
            payload = {"success": True, "result": {"id": 1, "state": "closed"}}
        elif path == "/v2/positions":
            payload = {"success": True, "result": []}
        else:
            raise AssertionError(f"unexpected {method} {path}")
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def test_safety_check_endpoint_reports_failures(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    broker_account, instrument = await _seed_live_ready(db_session, admin)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Draft Strategy For Safety Check", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.get(f"/api/v1/live-trading/safety-check?strategy_id={strategy_id}&broker_account_id={broker_account.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["passed"] is False
    assert body["checks"]["strategy_approved"] is False


async def test_cannot_start_live_deployment_without_confirmation(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    broker_account, instrument = await _seed_live_ready(db_session, admin)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Unconfirmed Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post(
        "/api/v1/live-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "broker_account_id": str(broker_account.id), "confirmed": False},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "confirmation" in resp.json()["detail"].lower()


async def test_cannot_start_live_deployment_when_not_approved(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    broker_account, instrument = await _seed_live_ready(db_session, admin)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Not Approved Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post(
        "/api/v1/live-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "broker_account_id": str(broker_account.id), "confirmed": True},
        headers=headers,
    )
    assert resp.status_code == 412  # safety checklist failed (status still "draft")


async def test_full_live_trading_flow_when_fully_ready(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession, monkeypatch):
    _patch_delta_ok(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    broker_account, instrument = await _seed_live_ready(db_session, admin)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Fully Ready Strategy",
            "version": {
                "python_code": 'def generate_signal(c,p):\n    return "HOLD"',
                "risk_rules": {"stop_loss_pct": 5.0},
                "position_sizing": {"type": "fixed_quantity", "value": 1},
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    # Manually walk the state machine to APPROVED, same as a human would via
    # backtest -> paper trade -> mark-validated -> approve.
    from app.models.strategy import Strategy
    strategy = await db_session.get(Strategy, uuid.UUID(strategy_id))
    strategy.status = "paper_trading"
    await db_session.commit()
    await client.post(f"/api/v1/strategies/{strategy_id}/mark-validated", headers=headers)
    await client.post(f"/api/v1/strategies/{strategy_id}/approve", headers=headers)

    deploy_resp = await client.post(
        "/api/v1/live-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "broker_account_id": str(broker_account.id), "confirmed": True},
        headers=headers,
    )
    assert deploy_resp.status_code == 201, deploy_resp.json()
    deployment_id = deploy_resp.json()["id"]

    strategy_after = await client.get(f"/api/v1/strategies/{strategy_id}", headers=headers)
    assert strategy_after.json()["status"] == "live"

    eval_resp = await client.post(f"/api/v1/live-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.status_code == 200
    assert eval_resp.json()["action"] == "hold"  # strategy always returns HOLD

    stop_resp = await client.post(f"/api/v1/live-trading/deployments/{deployment_id}/stop", headers=headers)
    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "stopped"


async def test_orders_and_trades_and_deployment_risk_fields(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession, monkeypatch):
    """Covers the new cross-deployment Orders/Trades endpoints and the
    risk_rules/realized_pnl_today fields added to LiveDeploymentOut --
    the data these back the Orders/Positions/Portfolio/Risk pages with."""
    _patch_delta_ok(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    broker_account, instrument = await _seed_live_ready(db_session, admin)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Always Buy Strategy",
            "version": {
                "python_code": 'def generate_signal(c,p):\n    return "BUY"',
                "risk_rules": {"stop_loss_pct": 5.0, "max_daily_loss_pct": 10.0},
                "position_sizing": {"type": "fixed_quantity", "value": 1},
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    from app.models.strategy import Strategy
    strategy = await db_session.get(Strategy, uuid.UUID(strategy_id))
    strategy.status = "paper_trading"
    await db_session.commit()
    await client.post(f"/api/v1/strategies/{strategy_id}/mark-validated", headers=headers)
    await client.post(f"/api/v1/strategies/{strategy_id}/approve", headers=headers)

    deploy_resp = await client.post(
        "/api/v1/live-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "broker_account_id": str(broker_account.id), "confirmed": True},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    eval_resp = await client.post(f"/api/v1/live-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.json()["action"] == "entered"

    # Deployment now carries its configured risk limits and today's real
    # (zero so far -- nothing closed yet) realized P&L.
    deployments = await client.get("/api/v1/live-trading/deployments", headers=headers)
    dep = next(d for d in deployments.json() if d["id"] == deployment_id)
    assert dep["risk_rules"]["stop_loss_pct"] == 5.0
    assert dep["risk_rules"]["max_daily_loss_pct"] == 10.0
    assert dep["realized_pnl_today"] == 0.0
    assert dep["open_position"]["quantity"] == 1

    # Cross-deployment orders listing (no deployment_id) finds it, with
    # context fields a single-deployment view wouldn't need to repeat.
    all_orders = await client.get("/api/v1/live-trading/orders", headers=headers)
    assert all_orders.status_code == 200
    matching = [o for o in all_orders.json() if o["deployment_id"] == deployment_id]
    assert len(matching) == 1
    assert matching[0]["strategy_name"] == "Always Buy Strategy"
    assert matching[0]["instrument_symbol"] == "APIX"

    # Trades listing is empty -- the position is still open, nothing has
    # closed yet, so there's nothing to show as a realized round-trip.
    trades = await client.get("/api/v1/live-trading/trades", headers=headers)
    assert trades.status_code == 200
    assert trades.json() == []


async def test_broker_account_balance_endpoint(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession, monkeypatch):
    _patch_delta_ok(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    broker_account, _instrument = await _seed_live_ready(db_session, admin)

    resp = await client.get(f"/api/v1/brokers/accounts/{broker_account.id}/balance", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["available_margin"] == 100000.0
    assert body["currency"] == "USD"


async def test_kill_switch_activate_stops_all_active_deployments(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession, monkeypatch):
    _patch_delta_ok(monkeypatch)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    broker_account, instrument = await _seed_live_ready(db_session, admin)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Kill Switch Target",
            "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"', "risk_rules": {"stop_loss_pct": 5.0}, "position_sizing": {"type": "fixed_quantity", "value": 1}},
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    from app.models.strategy import Strategy
    strategy = await db_session.get(Strategy, uuid.UUID(strategy_id))
    strategy.status = "paper_trading"
    await db_session.commit()
    await client.post(f"/api/v1/strategies/{strategy_id}/mark-validated", headers=headers)
    await client.post(f"/api/v1/strategies/{strategy_id}/approve", headers=headers)

    deploy_resp = await client.post(
        "/api/v1/live-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "broker_account_id": str(broker_account.id), "confirmed": True},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    kill_resp = await client.post("/api/v1/live-trading/kill-switch/activate", json={"reason": "test emergency"}, headers=headers)
    assert kill_resp.status_code == 200
    assert kill_resp.json()["active"] is True

    deployments = await client.get("/api/v1/live-trading/deployments", headers=headers)
    assert next(d for d in deployments.json() if d["id"] == deployment_id)["status"] == "stopped"

    eval_resp = await client.post(f"/api/v1/live-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.json()["action"] == "blocked"

    deactivate_resp = await client.post("/api/v1/live-trading/kill-switch/deactivate", headers=headers)
    assert deactivate_resp.json()["active"] is False


async def test_non_admin_cannot_activate_kill_switch(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "KillSwitchPass1!"
    trader = User(email="killswitchtrader@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader")
    trader.user_roles = [UserRole(role=trader_role)]
    db_session.add(trader)
    await db_session.commit()

    token = await _login(client, "killswitchtrader@tradingmaster.internal", password)
    resp = await client.post("/api/v1/live-trading/kill-switch/activate", json={"reason": "unauthorized attempt"}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
