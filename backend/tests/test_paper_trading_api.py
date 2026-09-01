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


async def _default_portfolio_id(client: AsyncClient, headers: dict) -> str:
    """The lazily-created default pool -- most tests just need any pool to
    deploy into and don't care about multi-pool behavior specifically."""
    resp = await client.get("/api/v1/paper-trading/portfolios", headers=headers)
    return resp.json()[0]["id"]


async def test_full_paper_trading_flow_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Paper API Strategy", "version": {"entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]}, "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]}}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id, "timeframe": "1d"},
        headers=headers,
    )
    assert deploy_resp.status_code == 201
    deployment_id = deploy_resp.json()["id"]
    assert deploy_resp.json()["status"] == "active"
    assert deploy_resp.json()["portfolio_id"] == portfolio_id

    eval_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.status_code == 200
    assert eval_resp.json()["action"] == "entered"

    portfolios_resp = await client.get("/api/v1/paper-trading/portfolios", headers=headers)
    portfolio = next(p for p in portfolios_resp.json() if p["id"] == portfolio_id)
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


async def test_manual_exit_closes_open_position(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Manual Exit Strategy",
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
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id, "timeframe": "1d"},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    # Entry rule is always-true, exit rule always-false -- the position
    # would never close on its own, so only a manual exit can end it.
    enter_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert enter_resp.json()["action"] == "entered"

    exit_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/exit", headers=headers)
    assert exit_resp.status_code == 200
    assert exit_resp.json()["action"] == "exited"

    list_resp = await client.get("/api/v1/paper-trading/deployments", headers=headers)
    deployment = next(d for d in list_resp.json() if d["id"] == deployment_id)
    assert deployment["open_position"] is None
    assert deployment["status"] == "active"  # exiting a position doesn't stop the deployment


async def test_manual_exit_rejects_when_flat(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Flat Exit Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id, "timeframe": "1d"},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    exit_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/exit", headers=headers)
    assert exit_resp.status_code == 200
    assert exit_resp.json()["action"] == "error"
    assert "no open position" in exit_resp.json()["reason"]


async def test_create_portfolio_with_currency_and_deploy_against_it(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/paper-trading/portfolios",
        json={"name": "Delta USD Pool", "currency": "USD", "initial_capital": 20000},
        headers=headers,
    )
    assert create_resp.status_code == 201
    pool = create_resp.json()
    assert pool["currency"] == "USD"
    assert pool["cash"] == 20000.0

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "USD Pool Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": pool["id"], "timeframe": "1d"},
        headers=headers,
    )
    assert deploy_resp.status_code == 201
    assert deploy_resp.json()["currency"] == "USD"
    assert deploy_resp.json()["portfolio_name"] == "Delta USD Pool"


async def test_list_portfolios_keeps_pools_isolated(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    inr_id = await _default_portfolio_id(client, headers)
    usd_resp = await client.post(
        "/api/v1/paper-trading/portfolios", json={"name": "USD Pool", "currency": "USD", "initial_capital": 5000}, headers=headers
    )
    usd_id = usd_resp.json()["id"]

    await client.patch(f"/api/v1/paper-trading/portfolios/{inr_id}", json={"initial_capital": 250000}, headers=headers)

    listed = (await client.get("/api/v1/paper-trading/portfolios", headers=headers)).json()
    by_id = {p["id"]: p for p in listed}
    assert len(listed) == 2
    assert by_id[inr_id]["cash"] == 250000.0
    assert by_id[usd_id]["cash"] == 5000.0
    assert by_id[usd_id]["currency"] == "USD"


async def test_delete_empty_portfolio_removes_it(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/paper-trading/portfolios", json={"name": "Throwaway Pool", "currency": "USD", "initial_capital": 1000}, headers=headers
    )
    pool_id = create_resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/paper-trading/portfolios/{pool_id}", headers=headers)
    assert delete_resp.status_code == 204

    listed = (await client.get("/api/v1/paper-trading/portfolios", headers=headers)).json()
    assert not any(p["id"] == pool_id for p in listed)


async def test_delete_portfolio_cleans_up_stopped_deployments(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    pool_id = (
        await client.post(
            "/api/v1/paper-trading/portfolios", json={"name": "Cleanup Pool", "currency": "INR", "initial_capital": 100000}, headers=headers
        )
    ).json()["id"]
    strategy_id = (
        await client.post(
            "/api/v1/strategies",
            json={"name": "Cleanup Pool Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
            headers=headers,
        )
    ).json()["id"]
    deployment_id = (
        await client.post(
            "/api/v1/paper-trading/deployments",
            json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": pool_id, "timeframe": "1d"},
            headers=headers,
        )
    ).json()["id"]
    await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/stop", headers=headers)

    delete_resp = await client.delete(f"/api/v1/paper-trading/portfolios/{pool_id}", headers=headers)
    assert delete_resp.status_code == 204

    list_resp = await client.get("/api/v1/paper-trading/deployments", headers=headers)
    assert not any(d["id"] == deployment_id for d in list_resp.json())


async def test_delete_portfolio_with_active_deployment_and_open_position(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    """Paper trading has no real position to unwind -- deleting a pool
    force-removes active deployments and any open positions in it, no
    need to stop each one by hand first."""
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    pool_id = (
        await client.post(
            "/api/v1/paper-trading/portfolios", json={"name": "Active Pool", "currency": "INR", "initial_capital": 100000}, headers=headers
        )
    ).json()["id"]
    strategy_id = (
        await client.post(
            "/api/v1/strategies",
            json={
                "name": "Active Pool Strategy",
                "version": {
                    "entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]},
                    "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]},
                },
            },
            headers=headers,
        )
    ).json()["id"]
    deployment_id = (
        await client.post(
            "/api/v1/paper-trading/deployments",
            json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": pool_id, "timeframe": "1d"},
            headers=headers,
        )
    ).json()["id"]

    # Open a real position so the pool being deleted isn't just active-but-flat.
    enter_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert enter_resp.json()["action"] == "entered"

    delete_resp = await client.delete(f"/api/v1/paper-trading/portfolios/{pool_id}", headers=headers)
    assert delete_resp.status_code == 204

    listed = (await client.get("/api/v1/paper-trading/portfolios", headers=headers)).json()
    assert not any(p["id"] == pool_id for p in listed)
    deployments = (await client.get("/api/v1/paper-trading/deployments", headers=headers)).json()
    assert not any(d["id"] == deployment_id for d in deployments)


async def test_update_portfolio_capital_rejects_non_positive(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)
    resp = await client.patch(f"/api/v1/paper-trading/portfolios/{portfolio_id}", json={"initial_capital": 0}, headers=headers)
    assert resp.status_code == 422


async def test_cannot_delete_active_deployment(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Delete Guard Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id, "timeframe": "1d"},
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
    portfolio_id = await _default_portfolio_id(client, headers)

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
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id, "timeframe": "1d"},
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
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    portfolio_id = await _default_portfolio_id(client, admin_headers)
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Owned Paper Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=admin_headers,
    )
    strategy_id = strategy_resp.json()["id"]

    other_token = await _login(client, "ptuser@tradingmaster.internal", password)
    resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


async def test_rejected_order_visible_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

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
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    eval_resp = await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.json()["action"] == "rejected"
    assert "Max open positions" in eval_resp.json()["reason"]

    orders_resp = await client.get(f"/api/v1/paper-trading/orders?deployment_id={deployment_id}", headers=headers)
    assert orders_resp.json()[0]["status"] == "rejected"
