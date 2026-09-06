from datetime import date, datetime, timezone

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.backfill_platform.timeframes import timeframes_for_source
from app.services.market_data import yahoo_source as yahoo_source_module

_original_get = httpx.AsyncClient.get


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def test_yahoo_timeframes_are_its_real_native_set():
    options = timeframes_for_source("yahoo")
    native_values = {o.value for o in options if o.native}
    assert native_values == {"1m", "5m", "15m", "30m", "60m", "1d", "1wk", "1mo"}


def test_delta_timeframes_include_4h_native_no_native_monthly():
    options = timeframes_for_source("delta")
    native_values = {o.value for o in options if o.native}
    assert "4h" in native_values
    assert "1mo" not in native_values
    derived = {o.value for o in options if not o.native}
    assert "1mo" in derived  # available as a resample of daily bars


def test_zerodha_timeframes_have_no_native_weekly_or_monthly():
    options = timeframes_for_source("zerodha")
    native_values = {o.value for o in options if o.native}
    assert "1wk" not in native_values
    assert "1mo" not in native_values
    derived = {o.value for o in options if not o.native}
    assert derived == {"1wk", "1mo"}


async def test_timeframes_endpoint_returns_real_per_source_list(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/delta/timeframes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    values = {o["value"] for o in resp.json()}
    assert "4h" in values
    assert "45m" not in values  # not a real interval on any source


async def test_resampled_candles_derive_weekly_from_stored_daily_bars(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from app.models.backfill_platform import BfOhlcvBar, BfSymbol

    symbol = BfSymbol(source="delta", symbol="NVDAXUSD", display_name="NVIDIA xStock Token")
    db_session.add(symbol)
    await db_session.flush()
    for i in range(10):
        db_session.add(BfOhlcvBar(
            symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1 + i, tzinfo=timezone.utc),
            open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=1000,
        ))
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get(
        "/api/v1/backfill-platform/candles/resampled",
        params={"source": "delta", "symbol": "NVDAXUSD", "target_timeframe": "1wk"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


async def test_bulk_backfill_queues_a_job_per_symbol(client: AsyncClient, seeded_admin: dict, monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        if "delta.exchange" not in str(url):
            return await _original_get(client_self, url, **kwargs)
        if str(url).endswith("/v2/products"):
            return httpx.Response(200, json={"success": True, "result": [
                {"symbol": "NVDAXUSD", "description": "NVIDIA xStock Token"},
                {"symbol": "PLTRXUSD", "description": "Palantir Technologies xStock Token"},
            ]}, request=httpx.Request("GET", str(url)))
        return httpx.Response(200, json={"success": True, "result": []}, request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post(
        "/api/v1/backfill-platform/sources/delta/backfill-all",
        params={"timeframe": "1d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    assert resp.json()["queued"] == 2

    jobs_resp = await client.get("/api/v1/backfill-platform/jobs?source=delta", headers={"Authorization": f"Bearer {token}"})
    assert len(jobs_resp.json()) == 2


async def test_bulk_backfill_requires_administrator(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    trader = User(email="trader_bulk@tradingmaster.internal", hashed_password=hash_password("TraderPass123!"), full_name="Trader")
    trader.user_roles = [UserRole(role=role)]
    db_session.add(trader)
    await db_session.commit()

    login_resp = await client.post("/api/v1/auth/login", json={"email": "trader_bulk@tradingmaster.internal", "password": "TraderPass123!"})
    token = login_resp.json()["access_token"]
    resp = await client.post("/api/v1/backfill-platform/sources/delta/backfill-all", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_bulk_backfill_rejects_retired_yahoo_source(client: AsyncClient, seeded_admin: dict):
    """Yahoo's NSE coverage is retired now that Zerodha backfills the same
    instruments -- bulk-all must reject it outright rather than re-pulling
    into the same shared Instrument row Zerodha now owns."""
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post("/api/v1/backfill-platform/sources/yahoo/backfill-all", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
    assert "retired" in resp.json()["detail"].lower()


async def test_single_job_creation_rejects_retired_yahoo_source(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={"source": "yahoo", "symbol": "RELIANCE", "display_name": "Reliance Industries", "timeframe": "1d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "retired" in resp.json()["detail"].lower()


async def test_live_sync_status_endpoint(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/live-sync/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert "running" in body
    assert "last_synced_count" in body
