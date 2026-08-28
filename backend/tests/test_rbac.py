from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.user import Role, User, UserRole


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_admin_can_list_users(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


async def test_viewer_cannot_list_users(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    viewer_role_result = await db_session.execute(Role.__table__.select().where(Role.name == "viewer"))
    viewer_role_row = viewer_role_result.first()
    viewer_role = await db_session.get(Role, viewer_role_row.id)

    password = "ViewerPass123!"
    viewer = User(email="viewer@tradingmaster.internal", hashed_password=hash_password(password), full_name="Viewer")
    viewer.user_roles = [UserRole(role=viewer_role)]
    db_session.add(viewer)
    await db_session.commit()

    token = await _login(client, "viewer@tradingmaster.internal", password)
    resp = await client.get("/api/v1/users", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_unauthenticated_request_rejected(client: AsyncClient):
    resp = await client.get("/api/v1/users")
    assert resp.status_code == 401
