import asyncio
import io

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.market_data import delta_source as delta_source_module

_original_request = httpx.AsyncClient.request
_original_get = httpx.AsyncClient.get


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _patch_delta_ohlcv(monkeypatch, rows):
    """rows: Delta's raw candle shape, e.g. {"time": <unix_ts>, "open":...,
    "high":..., "low":..., "close":..., "volume":...}."""

    async def fake_get(client_self, url, **kwargs):
        if "delta.exchange" not in str(url):
            return await _original_get(client_self, url, **kwargs)
        return httpx.Response(200, json={"success": True, "result": rows}, request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_zerodha_status_with_no_account_connected(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/zerodha/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert "connected" in body["detail"].lower() or "No Zerodha" in body["detail"]


async def test_search_delta_symbols_filters_by_query(client: AsyncClient, seeded_admin: dict, monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        if str(url).endswith("/v2/products"):
            return httpx.Response(200, json={"success": True, "result": [
                {"symbol": "RELIANCEUSD", "description": "Reliance Industries xStock Token"},
                {"symbol": "TCSUSD", "description": "Tata Consultancy xStock Token"},
            ]}, request=httpx.Request("GET", str(url)))
        return await _original_get(client_self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/delta/symbols", params={"q": "reliance"}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["symbol"] == "RELIANCEUSD"


async def test_create_and_complete_backfill_job(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_delta_ohlcv(monkeypatch, [
        {"time": 1704067200, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000},
        {"time": 1704153600, "open": 103.0, "high": 106.0, "low": 101.0, "close": 104.0, "volume": 1200},
    ])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={"source": "delta", "symbol": "RELIANCE", "display_name": "Reliance Industries", "timeframe": "1d"},
        headers=headers,
    )
    assert create_resp.status_code == 202
    job_id = create_resp.json()["id"]

    # BackgroundTasks run inline after the response in tests (see conftest's
    # AsyncSessionLocal monkeypatch for this module -- registered below)
    status_resp = await client.get(f"/api/v1/backfill-platform/jobs/{job_id}", headers=headers)
    assert status_resp.json()["status"] == "completed"
    assert status_resp.json()["inserted_count"] == 2


async def test_backfill_job_surfaces_source_error(client: AsyncClient, seeded_admin: dict, monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        if "delta.exchange" in str(url):
            raise httpx.ConnectError("refused")
        return await _original_get(client_self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={"source": "delta", "symbol": "FAILCASE", "display_name": "Fail Case", "timeframe": "1d"},
        headers=headers,
    )
    job_id = create_resp.json()["id"]
    status_resp = await client.get(f"/api/v1/backfill-platform/jobs/{job_id}", headers=headers)
    assert status_resp.json()["status"] == "failed"
    assert status_resp.json()["error_message"]


async def test_create_nfo_backfill_job_stores_fo_metadata(client: AsyncClient, seeded_admin: dict):
    """Covers a real production bug: source="zerodha_nfo" (11 chars) was
    rejected by Postgres -- bf_symbols.source/bf_backfill_jobs.source were
    VARCHAR(10), one character short. NOTE: this test alone can't catch a
    regression of that specific bug, since the test suite runs on SQLite,
    which doesn't enforce VARCHAR length limits the way Postgres does (the
    real bug only ever surfaced in production) -- it still verifies the
    endpoint accepts the source and the F&O metadata round-trips
    correctly. The actual column-width fix was verified against a real
    local Postgres instance via `alembic upgrade head` + `alembic check`."""
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={
            "source": "zerodha_nfo", "symbol": "NIFTY26SEP23500CE", "display_name": "NIFTY26SEP23500CE", "timeframe": "15m",
            "expiry": "2026-09-29", "strike": 23500.0, "option_type": "CE", "lot_size": 65, "underlying_symbol": "NIFTY",
        },
        headers=headers,
    )
    assert create_resp.status_code == 202
    body = create_resp.json()
    assert body["source"] == "zerodha_nfo"
    assert body["symbol"] == "NIFTY26SEP23500CE"


async def test_watchlist_crud_and_items(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post("/api/v1/backfill-platform/watchlists", json={"name": "NSE Alpha", "tags": ["nse"]}, headers=headers)
    assert create_resp.status_code == 201
    wl = create_resp.json()
    assert wl["symbol_count"] == 0

    add_resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{wl['id']}/items",
        json={"source": "zerodha", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
        headers=headers,
    )
    assert add_resp.status_code == 201

    dup_resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{wl['id']}/items",
        json={"source": "zerodha", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
        headers=headers,
    )
    assert dup_resp.status_code == 409

    items_resp = await client.get(f"/api/v1/backfill-platform/watchlists/{wl['id']}/items", headers=headers)
    assert len(items_resp.json()) == 1

    list_resp = await client.get("/api/v1/backfill-platform/watchlists", headers=headers)
    assert list_resp.json()[0]["symbol_count"] == 1

    rename_resp = await client.patch(f"/api/v1/backfill-platform/watchlists/{wl['id']}", json={"name": "NSE Alpha 50", "tags": []}, headers=headers)
    assert rename_resp.json()["name"] == "NSE Alpha 50"

    item_id = items_resp.json()[0]["id"]
    remove_resp = await client.delete(f"/api/v1/backfill-platform/watchlists/{wl['id']}/items/{item_id}", headers=headers)
    assert remove_resp.status_code == 204

    delete_resp = await client.delete(f"/api/v1/backfill-platform/watchlists/{wl['id']}", headers=headers)
    assert delete_resp.status_code == 204


async def test_watchlist_bulk_add_items(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post("/api/v1/backfill-platform/watchlists", json={"name": "Bulk WL", "tags": []}, headers=headers)
    wl_id = create_resp.json()["id"]

    bulk_resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{wl_id}/items/bulk",
        json={"items": [
            {"source": "zerodha", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
            {"source": "zerodha", "symbol": "TCS", "display_name": "Tata Consultancy"},
        ]},
        headers=headers,
    )
    assert bulk_resp.status_code == 200
    body = bulk_resp.json()
    assert body["added"] == 2
    assert body["skipped"] == 0

    items_resp = await client.get(f"/api/v1/backfill-platform/watchlists/{wl_id}/items", headers=headers)
    assert len(items_resp.json()) == 2

    # Re-adding the same two (plus one new) skips the duplicates
    second_resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{wl_id}/items/bulk",
        json={"items": [
            {"source": "zerodha", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
            {"source": "zerodha", "symbol": "INFY", "display_name": "Infosys"},
        ]},
        headers=headers,
    )
    assert second_resp.json() == {"added": 1, "skipped": 1}


async def test_watchlist_bulk_add_requires_ownership(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select as sa_select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    wl_id = (await client.post("/api/v1/backfill-platform/watchlists", json={"name": "Owner Only", "tags": []}, headers=headers)).json()["id"]

    role = (await db_session.execute(sa_select(Role).where(Role.name == "trader"))).scalar_one()
    other = User(email="other_bulk@tradingmaster.internal", hashed_password=hash_password("OtherPass123!"), full_name="Other")
    other.user_roles = [UserRole(role=role)]
    db_session.add(other)
    await db_session.commit()
    other_token = (await client.post("/api/v1/auth/login", json={"email": "other_bulk@tradingmaster.internal", "password": "OtherPass123!"})).json()["access_token"]

    resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{wl_id}/items/bulk",
        json={"items": [{"source": "zerodha", "symbol": "RELIANCE", "display_name": "Reliance Industries"}]},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 404


async def test_watchlist_isolated_per_owner(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    other = User(email="other_wl@tradingmaster.internal", hashed_password=hash_password("OtherPass123!"), full_name="Other")
    other.user_roles = [UserRole(role=role)]
    db_session.add(other)
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    create_resp = await client.post("/api/v1/backfill-platform/watchlists", json={"name": "Private List", "tags": []}, headers={"Authorization": f"Bearer {token}"})
    wl_id = create_resp.json()["id"]

    other_token = await _login(client, "other_wl@tradingmaster.internal", "OtherPass123!")
    resp = await client.get(f"/api/v1/backfill-platform/watchlists/{wl_id}/items", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 404


async def test_watchlist_csv_import_and_export(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    wl_id = (await client.post("/api/v1/backfill-platform/watchlists", json={"name": "CSV Test", "tags": []}, headers=headers)).json()["id"]

    csv_content = "source,symbol,display_name\nzerodha,RELIANCE,Reliance Industries\ndelta,BTCUSD,Bitcoin Perpetual\n"
    files = {"file": ("watchlist.csv", io.BytesIO(csv_content.encode()), "text/csv")}
    import_resp = await client.post(f"/api/v1/backfill-platform/watchlists/{wl_id}/import", files=files, headers=headers)
    assert import_resp.status_code == 200
    assert import_resp.json()["added"] == 2

    export_resp = await client.get(f"/api/v1/backfill-platform/watchlists/{wl_id}/export.csv", headers=headers)
    assert export_resp.status_code == 200
    assert "RELIANCE" in export_resp.text
    assert "BTCUSD" in export_resp.text


async def test_completeness_marks_weekends_correctly_for_zerodha(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    # 2024-01-01 is a Monday, 2024-01-07 is a Sunday; no bars stored -- every
    # weekday in range should show as a gap, weekends silently excluded.
    resp = await client.get(
        "/api/v1/backfill-platform/completeness",
        params={"source": "zerodha", "symbol": "NEVERBACKFILLED", "timeframe": "1d", "start": "2024-01-01", "end": "2024-01-07"},
        headers=headers,
    )
    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert all(s["status"] == "gap" for s in segments)
    total_days = sum((__import__("datetime").date.fromisoformat(s["end"]) - __import__("datetime").date.fromisoformat(s["start"])).days + 1 for s in segments)
    assert total_days == 5  # Mon-Fri only


async def test_export_symbol_xlsx_returns_real_workbook(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_delta_ohlcv(monkeypatch, [
        {"time": 1704067200, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "volume": 1000},
    ])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={"source": "delta", "symbol": "XLSXTEST", "display_name": "Xlsx Test", "timeframe": "1d"},
        headers=headers,
    )
    assert create_resp.json()

    resp = await client.get("/api/v1/backfill-platform/export/symbol.xlsx", params={"source": "delta", "symbol": "XLSXTEST", "timeframe": "1d"}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.content[:2] == b"PK"  # xlsx is a real zip archive


async def test_export_unknown_symbol_returns_404(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get(
        "/api/v1/backfill-platform/export/symbol.xlsx", params={"source": "zerodha", "symbol": "NEVERTRACKED", "timeframe": "1d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_invalid_source_rejected(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/nasdaq/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400


async def test_concurrent_jobs_for_a_brand_new_symbol_do_not_crash(client: AsyncClient, seeded_admin: dict, monkeypatch):
    """Reproduces the real production incident: selecting several
    timeframes for one symbol fires one POST /jobs per timeframe in
    parallel, and every one of them races to create the same brand-new
    bf_symbols row. Before the ON CONFLICT DO NOTHING fix in
    get_or_create_symbol, the loser of that race 500'd on a
    UniqueViolationError instead of just reusing the winner's row."""
    _patch_delta_ohlcv(monkeypatch, [])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    payload_base = {"source": "delta", "symbol": "RACENEW", "display_name": "Race New Co"}
    responses = await asyncio.gather(
        *[
            client.post("/api/v1/backfill-platform/jobs", json={**payload_base, "timeframe": tf}, headers=headers)
            for tf in ["1d", "15m", "60m"]
        ]
    )
    assert [r.status_code for r in responses] == [202, 202, 202]
    assert len({r.json()["id"] for r in responses}) == 3  # three distinct jobs, one shared symbol


async def test_watchlist_backfill_scoped_to_selected_item_ids(client: AsyncClient, seeded_admin: dict, monkeypatch):
    """The checkbox-select UI passes item_ids to run the watchlist backfill
    on just the checked symbols instead of every item."""
    _patch_delta_ohlcv(monkeypatch, [])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    wl_resp = await client.post("/api/v1/backfill-platform/watchlists", json={"name": "Two Symbols", "tags": []}, headers=headers)
    watchlist_id = wl_resp.json()["id"]
    item1 = await client.post(
        f"/api/v1/backfill-platform/watchlists/{watchlist_id}/items",
        json={"source": "delta", "symbol": "RELIANCE", "display_name": "Reliance Industries"}, headers=headers,
    )
    await client.post(
        f"/api/v1/backfill-platform/watchlists/{watchlist_id}/items",
        json={"source": "delta", "symbol": "TCS", "display_name": "Tata Consultancy Services"}, headers=headers,
    )

    resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{watchlist_id}/backfill",
        params={"timeframe": "1d"},
        json={"item_ids": [item1.json()["id"]]},
        headers=headers,
    )
    assert resp.status_code == 202
    jobs = resp.json()
    assert len(jobs) == 1
    assert jobs[0]["symbol"] == "RELIANCE"


async def test_watchlist_backfill_skips_timeframe_unsupported_by_source(client: AsyncClient, seeded_admin: dict):
    """Zerodha has no native 1wk/1mo candle -- queuing a job for it would
    just fail against Kite's real API, so it's skipped instead."""
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    wl_resp = await client.post("/api/v1/backfill-platform/watchlists", json={"name": "Zerodha Only", "tags": []}, headers=headers)
    watchlist_id = wl_resp.json()["id"]
    await client.post(
        f"/api/v1/backfill-platform/watchlists/{watchlist_id}/items",
        json={"source": "zerodha", "symbol": "RELIANCE", "display_name": "Reliance Industries"}, headers=headers,
    )

    resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{watchlist_id}/backfill", params={"timeframe": "1mo"}, headers=headers,
    )
    assert resp.status_code == 202
    assert resp.json() == []


async def test_zerodha_backfill_all_only_queues_watchlisted_symbols(client: AsyncClient, seeded_admin: dict):
    """Kite's NSE instrument dump is the entire exchange (10,000+ rows) --
    "Backfill All Tracked Symbols" for Zerodha must never queue that whole
    catalog. It should queue exactly the symbols the user explicitly added
    to a watchlist, and it must do so without ever calling out to Kite's
    API (no broker connection needed to trigger a purely DB-scoped
    action)."""
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    wl_resp = await client.post(
        "/api/v1/backfill-platform/watchlists", json={"name": "My Zerodha Picks", "tags": []}, headers=headers
    )
    watchlist_id = wl_resp.json()["id"]
    item_resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{watchlist_id}/items",
        json={"source": "zerodha", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
        headers=headers,
    )
    assert item_resp.status_code == 201

    # No Kite account is connected in this test at all -- if backfill-all
    # still tried to reach Kite's real API for the full catalog (the old
    # behavior), this would 502 instead of the 202 asserted below.
    resp = await client.post(
        "/api/v1/backfill-platform/sources/zerodha/backfill-all", params={"timeframe": "1d"}, headers=headers
    )
    assert resp.status_code == 202
    assert resp.json()["queued"] == 1

    jobs_resp = await client.get("/api/v1/backfill-platform/jobs", params={"source": "zerodha"}, headers=headers)
    symbols_queued = {j["symbol"] for j in jobs_resp.json()}
    assert symbols_queued == {"RELIANCE"}
