from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User


async def test_login_success_returns_access_token_and_sets_refresh_cookie(client: AsyncClient, seeded_admin: dict):
    resp = await client.post("/api/v1/auth/login", json=seeded_admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert "refresh_token" in resp.cookies


async def test_login_wrong_password_rejected(client: AsyncClient, seeded_admin: dict):
    resp = await client.post("/api/v1/auth/login", json={"email": seeded_admin["email"], "password": "nope"})
    assert resp.status_code == 401


async def test_login_writes_audit_log(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    await client.post("/api/v1/auth/login", json=seeded_admin)
    result = await db_session.execute(select(AuditLog).where(AuditLog.action == "LOGIN"))
    assert result.scalar_one_or_none() is not None


async def test_me_requires_valid_token(client: AsyncClient):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_me_returns_current_user(client: AsyncClient, seeded_admin: dict):
    login_resp = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login_resp.json()["access_token"]
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == seeded_admin["email"]
    assert "administrator" in resp.json()["roles"]


async def test_refresh_rotates_token_and_old_cookie_fails(client: AsyncClient, seeded_admin: dict):
    login_resp = await client.post("/api/v1/auth/login", json=seeded_admin)
    old_cookie = login_resp.cookies["refresh_token"]

    refresh_resp = await client.post("/api/v1/auth/refresh")
    assert refresh_resp.status_code == 200

    client.cookies.set("refresh_token", old_cookie)
    reuse_resp = await client.post("/api/v1/auth/refresh")
    assert reuse_resp.status_code == 401


async def test_logout_revokes_session(client: AsyncClient, seeded_admin: dict):
    login_resp = await client.post("/api/v1/auth/login", json=seeded_admin)
    cookie = login_resp.cookies["refresh_token"]

    logout_resp = await client.post("/api/v1/auth/logout")
    assert logout_resp.status_code == 204

    client.cookies.set("refresh_token", cookie)
    refresh_resp = await client.post("/api/v1/auth/refresh")
    assert refresh_resp.status_code == 401


async def test_register_creates_pending_unapproved_user(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "newbie@example.com", "password": "NewbiePass123!", "full_name": "New Person"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_approved"] is False
    assert body["roles"] == []

    result = await db_session.execute(select(User).where(User.email == "newbie@example.com"))
    user = result.scalar_one()
    assert user.is_approved is False


async def test_register_duplicate_email_rejected(client: AsyncClient, seeded_admin: dict):
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": seeded_admin["email"], "password": "SomePass123!", "full_name": "Dupe"},
    )
    assert resp.status_code == 409


async def test_login_blocked_until_admin_approves(client: AsyncClient, seeded_admin: dict):
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "pending@example.com", "password": "PendingPass123!", "full_name": "Pending Person"},
    )
    assert register_resp.status_code == 201

    login_resp = await client.post(
        "/api/v1/auth/login", json={"email": "pending@example.com", "password": "PendingPass123!"}
    )
    assert login_resp.status_code == 403

    admin_token_resp = await client.post("/api/v1/auth/login", json=seeded_admin)
    admin_token = admin_token_resp.json()["access_token"]

    approve_resp = await client.post(
        f"/api/v1/users/{register_resp.json()['id']}/approve",
        json={"roles": ["viewer"]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["is_approved"] is True
    assert approve_resp.json()["roles"] == ["viewer"]

    login_after_approval = await client.post(
        "/api/v1/auth/login", json={"email": "pending@example.com", "password": "PendingPass123!"}
    )
    assert login_after_approval.status_code == 200


async def test_admin_can_reject_pending_user(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "reject-me@example.com", "password": "RejectPass123!", "full_name": "Reject Me"},
    )
    user_id = register_resp.json()["id"]

    admin_token_resp = await client.post("/api/v1/auth/login", json=seeded_admin)
    admin_token = admin_token_resp.json()["access_token"]

    reject_resp = await client.post(
        f"/api/v1/users/{user_id}/reject", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert reject_resp.status_code == 204

    result = await db_session.execute(select(User).where(User.email == "reject-me@example.com"))
    assert result.scalar_one_or_none() is None
