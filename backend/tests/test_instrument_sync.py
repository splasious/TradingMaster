from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.services.market_data import delta_source as delta_source_module
from app.services.market_data import yahoo_source as yahoo_source_module


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _patch_yahoo_symbols(monkeypatch, symbols):
    async def fake_list_symbols(self):
        return symbols

    monkeypatch.setattr(yahoo_source_module.YahooNSEDataSource, "list_symbols", fake_list_symbols)


def _patch_delta_products(monkeypatch, products):
    async def fake_list_products(self):
        return products

    monkeypatch.setattr(delta_source_module.DeltaExchangeDataSource, "list_products", fake_list_products)


async def test_sync_yahoo_nse_creates_new_instruments(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_yahoo_symbols(monkeypatch, [
        {"nse_code": "SYNCTEST1", "yahoo_ticker": "SYNCTEST1.NS", "name": "Sync Test One", "is_active": True},
        {"nse_code": "SYNCTEST2", "yahoo_ticker": "SYNCTEST2.NS", "name": "Sync Test Two", "is_active": True},
        {"nse_code": "NIFTY 50", "yahoo_ticker": "^NSEI", "name": "Nifty 50", "is_active": True},
    ])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post("/api/v1/instruments/sync/yahoo_nse", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_source"] == "yahoo_nse"
    assert body["found"] == 3
    assert body["created"] == 3
    assert body["skipped"] == 0


async def test_sync_yahoo_nse_skips_inactive_and_existing(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession, monkeypatch):
    db_session.add(Instrument(exchange="NSE", symbol="ALREADYTHERE", name="Already There", instrument_type="equity", data_source="yahoo_nse", external_ref="ALREADYTHERE"))
    await db_session.commit()

    _patch_yahoo_symbols(monkeypatch, [
        {"nse_code": "ALREADYTHERE", "yahoo_ticker": "ALREADYTHERE.NS", "name": "Already There", "is_active": True},
        {"nse_code": "DELISTEDCO", "yahoo_ticker": "DELISTEDCO.NS", "name": "Delisted Co", "is_active": False},
        {"nse_code": "FRESHONE", "yahoo_ticker": "FRESHONE.NS", "name": "Fresh One", "is_active": True},
    ])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post("/api/v1/instruments/sync/yahoo_nse", headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    assert body["found"] == 3
    assert body["created"] == 1  # only FRESHONE: ALREADYTHERE already exists, DELISTEDCO is inactive
    assert body["skipped"] == 2


async def test_sync_is_idempotent(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_yahoo_symbols(monkeypatch, [{"nse_code": "IDEMPOTENT1", "yahoo_ticker": "IDEMPOTENT1.NS", "name": "Idempotent One", "is_active": True}])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post("/api/v1/instruments/sync/yahoo_nse", headers=headers)
    assert first.json()["created"] == 1

    second = await client.post("/api/v1/instruments/sync/yahoo_nse", headers=headers)
    assert second.json()["created"] == 0
    assert second.json()["skipped"] == 1


async def test_sync_delta_exchange_creates_new_instruments(client: AsyncClient, seeded_admin: dict, monkeypatch):
    _patch_delta_products(monkeypatch, [
        {"symbol": "XRPUSD", "description": "XRP Perpetual"},
        {"symbol": "DOGEUSD", "description": "Dogecoin Perpetual"},
    ])
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post("/api/v1/instruments/sync/delta_exchange", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_source"] == "delta_exchange"
    assert body["found"] == 2
    assert body["created"] == 2

    symbols = (await client.get("/api/v1/instruments?exchange=DELTA", headers={"Authorization": f"Bearer {token}"})).json()
    assert any(i["symbol"] == "XRPUSD" for i in symbols)


async def test_sync_rejects_unknown_data_source(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post("/api/v1/instruments/sync/not_a_real_source", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400


async def test_sync_requires_administrator_role(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select as sa_select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    role = (await db_session.execute(sa_select(Role).where(Role.name == "trader"))).scalar_one()
    trader = User(email="trader_sync@tradingmaster.internal", hashed_password=hash_password("TraderPass123!"), full_name="Trader")
    trader.user_roles = [UserRole(role=role)]
    db_session.add(trader)
    await db_session.commit()

    login_resp = await client.post("/api/v1/auth/login", json={"email": "trader_sync@tradingmaster.internal", "password": "TraderPass123!"})
    token = login_resp.json()["access_token"]

    resp = await client.post("/api/v1/instruments/sync/yahoo_nse", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_sync_surfaces_unreachable_service_as_502(client: AsyncClient, seeded_admin: dict, monkeypatch):
    from app.services.market_data.base import MarketDataSourceError

    async def fake_list_symbols(self):
        raise MarketDataSourceError("Could not reach the nse-yahoo-data service.")

    monkeypatch.setattr(yahoo_source_module.YahooNSEDataSource, "list_symbols", fake_list_symbols)

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post("/api/v1/instruments/sync/yahoo_nse", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 502
