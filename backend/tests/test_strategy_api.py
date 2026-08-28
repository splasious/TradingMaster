import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.strategy import Strategy
from app.models.user import Role, User, UserRole
from app.services.strategy.state_machine import StrategyStatus


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_create_visual_strategy(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "EMA Cross",
            "version": {
                "timeframe": "1d",
                "entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]},
                "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]},
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["code_type"] == "visual"
    assert body["status"] == "draft"
    assert body["latest_version"]["version_number"] == 1


async def test_create_python_strategy(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Momentum Py",
            "version": {"python_code": 'def generate_signal(candles, params):\n    return "HOLD"'},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    assert resp.json()["code_type"] == "python"


async def test_create_strategy_rejects_both_or_neither(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    neither = await client.post("/api/v1/strategies", json={"name": "Bad", "version": {}}, headers=headers)
    assert neither.status_code == 422

    both = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Bad2",
            "version": {"entry_rules": {"field": "close", "operator": ">", "value": 0}, "python_code": "x=1"},
        },
        headers=headers,
    )
    assert both.status_code == 422


async def test_viewer_cannot_create_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    role_row = (await db_session.execute(select(Role).where(Role.name == "viewer"))).scalar_one()
    password = "ViewerPass123!"
    viewer = User(email="viewer2@tradingmaster.internal", hashed_password=hash_password(password), full_name="Viewer")
    viewer.user_roles = [UserRole(role=role_row)]
    db_session.add(viewer)
    await db_session.commit()

    token = await _login(client, "viewer2@tradingmaster.internal", password)
    resp = await client.post(
        "/api/v1/strategies",
        json={"name": "X", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_non_owner_cannot_edit_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "TraderPass123!"
    other = User(email="trader2@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader Two")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    admin_token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    create_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Admin Owned", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    strategy_id = create_resp.json()["id"]

    other_token = await _login(client, "trader2@tradingmaster.internal", password)
    resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/versions",
        json={"python_code": 'def generate_signal(c,p):\n    return "BUY"'},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


async def test_new_version_resets_status_to_draft(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    create_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Resettable", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers={"Authorization": f"Bearer {token}"},
    )
    strategy_id = create_resp.json()["id"]

    strategy = await db_session.get(Strategy, uuid.UUID(strategy_id))
    strategy.status = StrategyStatus.BACKTESTED.value
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/strategies/{strategy_id}/versions",
        json={"python_code": 'def generate_signal(c,p):\n    return "BUY"'},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"
    assert resp.json()["latest_version"]["version_number"] == 2


async def test_validate_python_strategy(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    create_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Validated Py",
            "version": {"python_code": 'def generate_signal(candles, params):\n    return "BUY"'},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    strategy_id = create_resp.json()["id"]

    resp = await client.post(f"/api/v1/strategies/{strategy_id}/validate", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["sample_signal"] == "BUY"


async def test_validate_dangerous_python_strategy_fails(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    create_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Dangerous",
            "version": {"python_code": 'import os\ndef generate_signal(c, p):\n    os.system("echo x")\n    return "HOLD"'},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    strategy_id = create_resp.json()["id"]

    resp = await client.post(f"/api/v1/strategies/{strategy_id}/validate", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


async def test_list_strategies_scoped_to_owner_for_non_admin(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "TraderPass456!"
    trader = User(email="trader3@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader Three")
    trader.user_roles = [UserRole(role=trader_role)]
    db_session.add(trader)
    await db_session.commit()

    admin_token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    await client.post(
        "/api/v1/strategies",
        json={"name": "Admin Only Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    trader_token = await _login(client, "trader3@tradingmaster.internal", password)
    list_resp = await client.get("/api/v1/strategies", headers={"Authorization": f"Bearer {trader_token}"})
    assert all(s["name"] != "Admin Only Strategy" for s in list_resp.json())
