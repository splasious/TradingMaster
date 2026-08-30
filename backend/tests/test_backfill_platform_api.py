import io

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.backfill_platform import status as status_service
from app.services.market_data import delta_source as delta_source_module
from app.services.market_data import yahoo_source as yahoo_source_module

_original_request = httpx.AsyncClient.request
_original_get = httpx.AsyncClient.get


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _patch_yahoo_ohlcv(monkeypatch, bars):
    async def fake_get(client_self, url, **kwargs):
        if "127.0.0.1:8800" not in str(url) and "yahoo" not in str(url):
            return await _original_get(client_self, url, **kwargs)
        if str(url).endswith("/symbols"):
            return httpx.Response(200, json=[{"nse_code": "RELIANCE", "yahoo_ticker": "RELIANCE.NS", "name": "Reliance Industries", "is_active": True}], request=httpx.Request("GET", str(url)))
        return httpx.Response(200, json=bars, request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_yahoo_status_reflects_real_reachability(client: AsyncClient, seeded_admin: dict, monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        if str(url).endswith("/health"):
            return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", str(url)))
        return await _original_get(client_self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/yahoo/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["connected"] is True


async def test_yahoo_status_reports_unreachable_honestly(client: AsyncClient, seeded_admin: dict, monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        if "127.0.0.1:8800" in str(url):
            raise httpx.ConnectError("refused")
        return await _original_get(client_self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/yahoo/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.json()["connected"] is False


async def test_zerodha_status_with_no_account_connected(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/zerodha/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert "connected" in body["detail"].lower() or "No Zerodha" in body["detail"]


async def test_search_yahoo_symbols_filters_by_query(client: AsyncClient, seeded_admin: dict, monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        if "127.0.0.1:8800" in str(url) and str(url).endswith("/symbols"):
            return httpx.Response(200, json=[
                {"nse_code": "RELIANCE", "yahoo_ticker": "RELIANCE.NS", "name": "Reliance Industries", "is_active": True},
                {"nse_code": "TCS", "yahoo_ticker": "TCS.NS", "name": "Tata Consultancy", "is_active": True},
            ], request=httpx.Request("GET", str(url)))
        return await _original_get(client_self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/yahoo/symbols", params={"q": "reliance"}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    results = resp.json()
    assert len(results) == 1
    assert results[0]["symbol"] == "RELIANCE"


async def test_create_and_complete_backfill_job(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_yahoo_ohlcv(monkeypatch, [
        {"ts": "2024-01-01T00:00:00+00:00", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "adj_close": 103.0, "volume": 1000},
        {"ts": "2024-01-02T00:00:00+00:00", "open": 103.0, "high": 106.0, "low": 101.0, "close": 104.0, "adj_close": 104.0, "volume": 1200},
    ])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={"source": "yahoo", "symbol": "RELIANCE", "display_name": "Reliance Industries", "timeframe": "1d"},
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
        if "127.0.0.1:8800" in str(url):
            raise httpx.ConnectError("refused")
        return await _original_get(client_self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={"source": "yahoo", "symbol": "FAILCASE", "display_name": "Fail Case", "timeframe": "1d"},
        headers=headers,
    )
    job_id = create_resp.json()["id"]
    status_resp = await client.get(f"/api/v1/backfill-platform/jobs/{job_id}", headers=headers)
    assert status_resp.json()["status"] == "failed"
    assert status_resp.json()["error_message"]


async def test_watchlist_crud_and_items(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_yahoo_ohlcv(monkeypatch, [])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post("/api/v1/backfill-platform/watchlists", json={"name": "NSE Alpha", "tags": ["nse"]}, headers=headers)
    assert create_resp.status_code == 201
    wl = create_resp.json()
    assert wl["symbol_count"] == 0

    add_resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{wl['id']}/items",
        json={"source": "yahoo", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
        headers=headers,
    )
    assert add_resp.status_code == 201

    dup_resp = await client.post(
        f"/api/v1/backfill-platform/watchlists/{wl['id']}/items",
        json={"source": "yahoo", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
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
            {"source": "yahoo", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
            {"source": "yahoo", "symbol": "TCS", "display_name": "Tata Consultancy"},
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
            {"source": "yahoo", "symbol": "RELIANCE", "display_name": "Reliance Industries"},
            {"source": "yahoo", "symbol": "INFY", "display_name": "Infosys"},
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
        json={"items": [{"source": "yahoo", "symbol": "RELIANCE", "display_name": "Reliance Industries"}]},
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

    csv_content = "source,symbol,display_name\nyahoo,RELIANCE,Reliance Industries\ndelta,BTCUSD,Bitcoin Perpetual\n"
    files = {"file": ("watchlist.csv", io.BytesIO(csv_content.encode()), "text/csv")}
    import_resp = await client.post(f"/api/v1/backfill-platform/watchlists/{wl_id}/import", files=files, headers=headers)
    assert import_resp.status_code == 200
    assert import_resp.json()["added"] == 2

    export_resp = await client.get(f"/api/v1/backfill-platform/watchlists/{wl_id}/export.csv", headers=headers)
    assert export_resp.status_code == 200
    assert "RELIANCE" in export_resp.text
    assert "BTCUSD" in export_resp.text


async def test_completeness_marks_weekends_correctly_for_yahoo(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    # 2024-01-01 is a Monday, 2024-01-07 is a Sunday; no bars stored -- every
    # weekday in range should show as a gap, weekends silently excluded.
    resp = await client.get(
        "/api/v1/backfill-platform/completeness",
        params={"source": "yahoo", "symbol": "NEVERBACKFILLED", "timeframe": "1d", "start": "2024-01-01", "end": "2024-01-07"},
        headers=headers,
    )
    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert all(s["status"] == "gap" for s in segments)
    total_days = sum((__import__("datetime").date.fromisoformat(s["end"]) - __import__("datetime").date.fromisoformat(s["start"])).days + 1 for s in segments)
    assert total_days == 5  # Mon-Fri only


async def test_export_symbol_xlsx_returns_real_workbook(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_yahoo_ohlcv(monkeypatch, [
        {"ts": "2024-01-01T00:00:00+00:00", "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0, "adj_close": 103.0, "volume": 1000},
    ])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    create_resp = await client.post(
        "/api/v1/backfill-platform/jobs",
        json={"source": "yahoo", "symbol": "XLSXTEST", "display_name": "Xlsx Test", "timeframe": "1d"},
        headers=headers,
    )
    assert create_resp.json()

    resp = await client.get("/api/v1/backfill-platform/export/symbol.xlsx", params={"source": "yahoo", "symbol": "XLSXTEST", "timeframe": "1d"}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.content[:2] == b"PK"  # xlsx is a real zip archive


async def test_export_unknown_symbol_returns_404(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get(
        "/api/v1/backfill-platform/export/symbol.xlsx", params={"source": "yahoo", "symbol": "NEVERTRACKED", "timeframe": "1d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_invalid_source_rejected(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.get("/api/v1/backfill-platform/sources/nasdaq/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
