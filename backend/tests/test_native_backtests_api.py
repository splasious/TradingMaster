from datetime import date, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# Any weekday outside 2026 (the only year seeded in nse_holidays.STATIC_HOLIDAYS)
# is a guaranteed trading day for these tests.
_TEST_DATE = date(2025, 6, 2)
if _TEST_DATE.weekday() >= 5:
    _TEST_DATE += timedelta(days=7 - _TEST_DATE.weekday())

TRIVIAL_NATIVE_CODE = (
    "async def evaluate(ctx):\n"
    "    ctx.state['calls'] = ctx.state.get('calls', 0) + 1\n"
    "    ctx.note('hold', signal='TEST', reason='ok')\n"
)


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _create_native_strategy(client: AsyncClient, headers: dict, name: str = "Native Backtest API Strategy") -> str:
    resp = await client.post(
        "/api/v1/strategies",
        json={"name": name, "version": {"python_code": TRIVIAL_NATIVE_CODE, "is_native": True}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["code_type"] == "native"
    return resp.json()["id"]


async def test_full_native_backtest_flow_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _create_native_strategy(client, headers)

    create_resp = await client.post(
        "/api/v1/native-backtests",
        json={"strategy_id": strategy_id, "start_date": str(_TEST_DATE), "end_date": str(_TEST_DATE), "initial_capital": 50000},
        headers=headers,
    )
    assert create_resp.status_code == 202, create_resp.text
    job_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "pending"

    # BackgroundTasks run synchronously within the ASGI test transport (see
    # test_backtest_api.py), so by the time the POST above returned, the
    # replay has already run to completion (no candles seeded, so the
    # trivial strategy above never trades -- this exercises the plumbing).
    job_resp = await client.get(f"/api/v1/native-backtests/{job_id}", headers=headers)
    assert job_resp.json()["status"] == "completed", job_resp.json()

    result_resp = await client.get(f"/api/v1/native-backtests/{job_id}/result", headers=headers)
    assert result_resp.status_code == 200
    assert result_resp.json()["metrics"]["trade_count"] == 0

    trades_resp = await client.get(f"/api/v1/native-backtests/{job_id}/trades", headers=headers)
    assert trades_resp.json() == []

    list_resp = await client.get(f"/api/v1/native-backtests?strategy_id={strategy_id}", headers=headers)
    assert any(j["id"] == job_id for j in list_resp.json())

    strategy_after = await client.get(f"/api/v1/strategies/{strategy_id}", headers=headers)
    assert strategy_after.json()["status"] == "backtested"

    delete_resp = await client.delete(f"/api/v1/native-backtests/{job_id}", headers=headers)
    assert delete_resp.status_code == 204

    job_after = await client.get(f"/api/v1/native-backtests/{job_id}", headers=headers)
    assert job_after.status_code == 404


async def test_native_backtest_rejects_non_native_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Plain Python Strategy", "version": {"python_code": "def generate_signal(candles, params):\n    return 'HOLD'\n"}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    assert strategy_resp.json()["code_type"] == "python"

    resp = await client.post(
        "/api/v1/native-backtests",
        json={"strategy_id": strategy_id, "start_date": str(_TEST_DATE), "end_date": str(_TEST_DATE)},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_native_backtest_rejects_end_date_before_start_date(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _create_native_strategy(client, headers)

    resp = await client.post(
        "/api/v1/native-backtests",
        json={"strategy_id": strategy_id, "start_date": str(_TEST_DATE), "end_date": str(_TEST_DATE - timedelta(days=1))},
        headers=headers,
    )
    assert resp.status_code == 422
