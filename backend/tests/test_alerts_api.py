from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import AlertSeverity, AlertType
from app.models.user import User
from app.services.alerts.service import create_alert


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_alerts_list_unread_count_and_mark_read(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()

    await create_alert(
        db_session, user_id=admin.id, alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO,
        title="Order filled", message="Bought 10 @ 100",
    )
    await create_alert(
        db_session, user_id=admin.id, alert_type=AlertType.STOP_LOSS_TRIGGERED.value, severity=AlertSeverity.WARNING,
        title="Stop loss hit", message="Closed at 95",
    )
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    list_resp = await client.get("/api/v1/alerts", headers=headers)
    assert list_resp.status_code == 200
    alerts = list_resp.json()
    assert len(alerts) == 2

    unread_resp = await client.get("/api/v1/alerts/unread-count", headers=headers)
    assert unread_resp.json()["unread_count"] == 2

    filtered_resp = await client.get("/api/v1/alerts", params={"severity": "warning"}, headers=headers)
    assert len(filtered_resp.json()) == 1
    assert filtered_resp.json()[0]["alert_type"] == "stop_loss_triggered"

    mark_resp = await client.post(f"/api/v1/alerts/{alerts[0]['id']}/read", headers=headers)
    assert mark_resp.status_code == 200
    assert mark_resp.json()["is_read"] is True

    unread_resp = await client.get("/api/v1/alerts/unread-count", headers=headers)
    assert unread_resp.json()["unread_count"] == 1

    mark_all_resp = await client.post("/api/v1/alerts/read-all", headers=headers)
    assert mark_all_resp.status_code == 204

    unread_resp = await client.get("/api/v1/alerts/unread-count", headers=headers)
    assert unread_resp.json()["unread_count"] == 0


async def test_alerts_are_scoped_to_the_owning_user(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    await create_alert(
        db_session, user_id=admin.id, alert_type=AlertType.SYSTEM_ERROR.value, severity=AlertSeverity.CRITICAL,
        title="Someone else's alert", message="Should not be visible to another user",
    )
    await db_session.commit()

    from app.core.security import hash_password
    from app.models.user import Role, UserRole

    role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    other = User(email="other@tradingmaster.internal", hashed_password=hash_password("OtherPass123!"), full_name="Other")
    other.user_roles = [UserRole(role=role)]
    db_session.add(other)
    await db_session.commit()

    token = await _login(client, "other@tradingmaster.internal", "OtherPass123!")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/api/v1/alerts", headers=headers)
    assert resp.json() == []
