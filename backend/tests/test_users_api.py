import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.broker import Broker, BrokerAccount
from app.models.strategy import Strategy
from app.models.user import Role, User, UserRole


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _make_user(db_session: AsyncSession, email: str, role_name: str, *, is_active: bool = True) -> tuple[User, str]:
    role = (await db_session.execute(select(Role).where(Role.name == role_name))).scalar_one()
    password = "SomePass123!"
    user = User(email=email, hashed_password=hash_password(password), full_name="Test User", is_active=is_active)
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user, password


async def test_admin_can_edit_another_users_name_and_roles(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    target, _ = await _make_user(db_session, "editme@tradingmaster.internal", "viewer")

    resp = await client.patch(f"/api/v1/users/{target.id}", json={"full_name": "Renamed User", "roles": ["trader"]}, headers=headers)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["full_name"] == "Renamed User"
    assert body["roles"] == ["trader"]


async def test_admin_can_deactivate_another_user_and_it_revokes_sessions(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    target, password = await _make_user(db_session, "deactivateme@tradingmaster.internal", "viewer")
    target_token = await _login(client, "deactivateme@tradingmaster.internal", password)

    resp = await client.patch(f"/api/v1/users/{target.id}", json={"is_active": False}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # The revoked session can no longer be used.
    me_resp = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {target_token}"})
    assert me_resp.status_code == 401


async def test_admin_cannot_remove_own_administrator_role(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()

    resp = await client.patch(f"/api/v1/users/{admin.id}", json={"roles": ["viewer"]}, headers=headers)
    assert resp.status_code == 400
    assert "own administrator role" in resp.json()["detail"]


async def test_admin_cannot_deactivate_self(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()

    resp = await client.patch(f"/api/v1/users/{admin.id}", json={"is_active": False}, headers=headers)
    assert resp.status_code == 400
    assert "own account" in resp.json()["detail"]


async def test_active_administrator_count_helper(db_session: AsyncSession, seeded_admin: dict):
    """Unit-level check of the guard `update_user`/`delete_user` both rely
    on. Not independently reachable end-to-end via the API for the
    role-removal/deactivation case specifically: calling either endpoint
    at all requires the caller to already be an administrator, so a
    non-self actor demoting/deactivating "the last admin" always leaves
    at least the caller themselves -- the only way to reach a true
    last-admin edit is on your OWN account, which the separate self-checks
    (tested above) already block first. This test exercises the counting
    logic directly instead of forcing an artificial end-to-end path."""
    from app.api.v1.endpoints.users import _active_administrator_count

    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    assert await _active_administrator_count(db_session) == 1
    assert await _active_administrator_count(db_session, exclude_user_id=admin.id) == 0

    second_admin, _ = await _make_user(db_session, "second-admin@tradingmaster.internal", "administrator")
    assert await _active_administrator_count(db_session) == 2
    assert await _active_administrator_count(db_session, exclude_user_id=admin.id) == 1
    assert await _active_administrator_count(db_session, exclude_user_id=second_admin.id) == 1


async def test_delete_user_with_no_owned_data_succeeds(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    target, _ = await _make_user(db_session, "deleteme@tradingmaster.internal", "viewer")

    resp = await client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert resp.status_code == 204

    # db_session.get() would return the stale cached object from this
    # session's own identity map (created earlier in this same test) --
    # a fresh select() actually hits the DB regardless of that cache.
    gone = (await db_session.execute(select(User).where(User.id == target.id))).scalar_one_or_none()
    assert gone is None


async def test_delete_user_blocked_when_owning_a_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    target, _ = await _make_user(db_session, "trader-with-strategy@tradingmaster.internal", "trader")
    db_session.add(Strategy(name="Owned Strategy", owner_id=target.id, code_type="python"))
    await db_session.commit()

    resp = await client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert resp.status_code == 409
    assert "Deactivate" in resp.json()["detail"]

    still_there = await db_session.get(User, target.id)
    assert still_there is not None


async def test_delete_user_blocked_when_owning_a_broker_account(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    target, _ = await _make_user(db_session, "trader-with-broker@tradingmaster.internal", "trader")
    broker = (await db_session.execute(select(Broker).where(Broker.code == "delta_exchange"))).scalar_one_or_none()
    if broker is None:
        broker = Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True)
        db_session.add(broker)
        await db_session.flush()
    db_session.add(BrokerAccount(user_id=target.id, broker_id=broker.id, account_label="x", environment="paper"))
    await db_session.commit()

    resp = await client.delete(f"/api/v1/users/{target.id}", headers=headers)
    assert resp.status_code == 409


async def test_cannot_delete_own_account(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()

    resp = await client.delete(f"/api/v1/users/{admin.id}", headers=headers)
    assert resp.status_code == 400
    assert "own account" in resp.json()["detail"]


async def test_deleting_one_of_two_administrators_succeeds(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    """Deleting an admin is fine as long as another one remains -- the
    "cannot delete the last administrator" guard (same shared helper unit-
    tested above) only refuses when it would leave zero, not one."""
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    second_admin, second_password = await _make_user(db_session, "second-admin@tradingmaster.internal", "administrator")
    second_token = await _login(client, "second-admin@tradingmaster.internal", second_password)

    resp = await client.delete(f"/api/v1/users/{admin.id}", headers={"Authorization": f"Bearer {second_token}"})
    assert resp.status_code == 204

    remaining = (await db_session.execute(
        select(User).join(UserRole).join(Role).where(Role.name == "administrator", User.is_active.is_(True))
    )).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == second_admin.id
