from httpx import AsyncClient


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


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
