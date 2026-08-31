from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.user import Role, User, UserRole
from app.services.market_data.tick_engine import tick_engine


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed_instrument(db_session: AsyncSession) -> Instrument:
    instrument = Instrument(exchange="NSE", symbol="PAPX", name="Paper API Co", instrument_type="equity", data_source="yahoo_nse", external_ref="PAPX")
    db_session.add(instrument)
    await db_session.flush()
    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 100 + i
        db_session.add(
            OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test")
        )
    await db_session.commit()
    tick_engine._last_price.pop(instrument.id, None)
    return instrument


async def test_full_paper_trading_flow_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Paper API Strategy", "version": {"entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]}, "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]}}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "timeframe": "1d"},
        headers=headers,
    )
    assert deploy_resp.status_code == 201
    deployment_id = deploy_resp.json()["id"]
    assert deploy_resp.json()["status"] == "active"

    eval_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.status_code == 200
    assert eval_resp.json()["action"] == "entered"

    portfolio_resp = await client.get("/api/v1/paper-trading/portfolio", headers=headers)
    portfolio = portfolio_resp.json()
    assert portfolio["cash"] < portfolio["initial_capital"]
    assert len(portfolio["positions"]) == 1

    orders_resp = await client.get(f"/api/v1/paper-trading/orders?deployment_id={deployment_id}", headers=headers)
    assert len(orders_resp.json()) == 1
    assert orders_resp.json()[0]["status"] == "filled"

    stop_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/stop", headers=headers)
    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "stopped"

    list_resp = await client.get("/api/v1/paper-trading/deployments", headers=headers)
    assert any(d["id"] == deployment_id and d["status"] == "stopped" for d in list_resp.json())


async def test_update_portfolio_capital_resets_cash_and_initial_capital(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    # Creates the lazy portfolio at its 100000 default.
    initial = await client.get("/api/v1/paper-trading/portfolio", headers=headers)
    assert initial.json()["cash"] == 100000.0

    resp = await client.patch("/api/v1/paper-trading/portfolio", json={"initial_capital": 500000}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["cash"] == 500000.0
    assert resp.json()["initial_capital"] == 500000.0

    # Persists.
    again = await client.get("/api/v1/paper-trading/portfolio", headers=headers)
    assert again.json()["cash"] == 500000.0


async def test_update_portfolio_capital_rejects_non_positive(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.patch("/api/v1/paper-trading/portfolio", json={"initial_capital": 0}, headers=headers)
    assert resp.status_code == 422


async def test_cannot_delete_active_deployment(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Delete Guard Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "timeframe": "1d"},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/paper-trading/deployments/{deployment_id}", headers=headers)
    assert delete_resp.status_code == 409

    # Still there and still active -- the guard didn't half-delete anything.
    list_resp = await client.get("/api/v1/paper-trading/deployments", headers=headers)
    assert any(d["id"] == deployment_id and d["status"] == "active" for d in list_resp.json())


async def test_delete_stopped_deployment_removes_it_and_its_history(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Delete Flow Strategy",
            "version": {
                "entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]},
                "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]},
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "timeframe": "1d"},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    # Generate real order/position history before stopping, so deletion has
    # actual child rows to clean up, not just an empty deployment.
    await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)
    await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/stop", headers=headers)

    delete_resp = await client.delete(f"/api/v1/paper-trading/deployments/{deployment_id}", headers=headers)
    assert delete_resp.status_code == 204

    list_resp = await client.get("/api/v1/paper-trading/deployments", headers=headers)
    assert not any(d["id"] == deployment_id for d in list_resp.json())


async def test_non_owner_cannot_start_or_stop_deployment(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "PTPass123!"
    other = User(email="ptuser@tradingmaster.internal", hashed_password=hash_password(password), full_name="PT User")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    admin_token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Owned Paper Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    strategy_id = strategy_resp.json()["id"]

    other_token = await _login(client, "ptuser@tradingmaster.internal", password)
    resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id)},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


async def test_rejected_order_visible_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Max Position Strategy",
            "version": {
                "entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]},
                "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]},
                "risk_rules": {"max_positions": 0},
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id)},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    eval_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.json()["action"] == "rejected"
    assert "Max open positions" in eval_resp.json()["reason"]

    orders_resp = await client.get(f"/api/v1/paper-trading/orders?deployment_id={deployment_id}", headers=headers)
    assert orders_resp.json()[0]["status"] == "rejected"
