import sqlite3

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.user import Role, User, UserRole
from app.services.backup import service as backup_service


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_admin_can_create_list_and_download_backup(client: AsyncClient, seeded_admin: dict, tmp_path, monkeypatch):
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setattr(backup_service, "BACKUP_DIR", tmp_path / "backups")

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post("/api/v1/backup", headers=headers)
    assert create_resp.status_code == 201
    filename = create_resp.json()["filename"]

    list_resp = await client.get("/api/v1/backup", headers=headers)
    assert list_resp.status_code == 200
    assert any(b["filename"] == filename for b in list_resp.json())

    download_resp = await client.get(f"/api/v1/backup/{filename}/download", headers=headers)
    assert download_resp.status_code == 200
    assert download_resp.content


async def test_non_admin_cannot_manage_backups(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    trader = User(email="trader_backup@tradingmaster.internal", hashed_password=hash_password("TraderPass123!"), full_name="Trader")
    trader.user_roles = [UserRole(role=role)]
    db_session.add(trader)
    await db_session.commit()

    token = await _login(client, "trader_backup@tradingmaster.internal", "TraderPass123!")
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get("/api/v1/backup", headers=headers)
    assert resp.status_code == 403
