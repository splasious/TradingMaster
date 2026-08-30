import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfOhlcvBar, BfSymbol, BfWatchlist, BfWatchlistItem
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform.catalog_sync import CatalogSyncError, sync_symbol_to_catalog


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_sync_symbol_creates_instrument_and_candles(db_session: AsyncSession):
    symbol = BfSymbol(source="yahoo", symbol="SYNCTEST", display_name="Sync Test Co")
    db_session.add(symbol)
    await db_session.flush()
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=2, low=0.5, close=1.5, volume=100))
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 2, tzinfo=timezone.utc), open=1.5, high=2.5, low=1, close=2, volume=150))
    await db_session.commit()

    result = await sync_symbol_to_catalog(db_session, symbol)
    await db_session.commit()

    assert result.instrument_created is True
    assert result.bars_synced == 2
    assert result.bars_skipped == 0

    instrument = (await db_session.execute(select(Instrument).where(Instrument.symbol == "SYNCTEST"))).scalar_one()
    assert instrument.exchange == "NSE"
    assert instrument.data_source == "yahoo_nse"

    candles = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id))).scalars().all()
    assert len(candles) == 2
    assert all(c.source == "bf_yahoo" for c in candles)


async def test_sync_symbol_is_idempotent(db_session: AsyncSession):
    symbol = BfSymbol(source="delta", symbol="IDEMPTEST", display_name="Idempotent Test")
    db_session.add(symbol)
    await db_session.flush()
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1m", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()

    first = await sync_symbol_to_catalog(db_session, symbol)
    await db_session.commit()
    second = await sync_symbol_to_catalog(db_session, symbol)
    await db_session.commit()

    assert first.bars_synced == 1
    assert second.bars_synced == 0
    assert second.bars_skipped == 1
    assert second.instrument_created is False

    instrument = (await db_session.execute(select(Instrument).where(Instrument.symbol == "IDEMPTEST"))).scalar_one()
    candles = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id))).scalars().all()
    assert len(candles) == 1


async def test_sync_symbol_reuses_existing_instrument(db_session: AsyncSession):
    existing = Instrument(exchange="NSE", symbol="ALREADYCATALOGED", name="Already Cataloged Co", instrument_type="equity", data_source="yahoo_nse", external_ref="ALREADYCATALOGED")
    db_session.add(existing)
    await db_session.flush()

    symbol = BfSymbol(source="yahoo", symbol="ALREADYCATALOGED", display_name="Already Cataloged Co")
    db_session.add(symbol)
    await db_session.flush()
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()

    result = await sync_symbol_to_catalog(db_session, symbol)

    assert result.instrument_created is False
    assert result.instrument_id == str(existing.id)


async def test_sync_symbol_rejects_unmapped_source(db_session: AsyncSession):
    symbol = BfSymbol(source="zerodha", symbol="NOTMAPPED", display_name="Not Mapped")
    db_session.add(symbol)
    await db_session.commit()

    try:
        await sync_symbol_to_catalog(db_session, symbol)
        assert False, "expected CatalogSyncError"
    except CatalogSyncError:
        pass


async def test_sync_single_symbol_endpoint(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    symbol = BfSymbol(source="delta", symbol="SINGLESYNC", display_name="Single Sync Co")
    db_session.add(symbol)
    await db_session.flush()
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()

    resp = await client.post("/api/v1/backfill-platform/sources/delta/symbols/SINGLESYNC/sync-to-catalog", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "SINGLESYNC"
    assert body["bars_synced"] == 1
    assert body["instrument_created"] is True

    instruments_resp = await client.get("/api/v1/instruments?q=SINGLESYNC", headers=headers)
    assert len(instruments_resp.json()) == 1


async def test_sync_single_symbol_404_when_no_backfilled_data(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.post(
        "/api/v1/backfill-platform/sources/delta/symbols/NEVERBACKFILLED/sync-to-catalog",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_sync_watchlist_to_catalog_endpoint(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    wl_resp = await client.post("/api/v1/backfill-platform/watchlists", json={"name": "Sync WL", "tags": []}, headers=headers)
    wl_id = wl_resp.json()["id"]

    symbol = BfSymbol(source="yahoo", symbol="ENDPOINTSYNC", display_name="Endpoint Sync Co")
    db_session.add(symbol)
    await db_session.flush()
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1d", ts=datetime(2024, 1, 1, tzinfo=timezone.utc), open=1, high=1, low=1, close=1, volume=1))
    await db_session.flush()

    wl = (await db_session.execute(select(BfWatchlist).where(BfWatchlist.id == uuid.UUID(wl_id)))).scalar_one()
    db_session.add(BfWatchlistItem(watchlist_id=wl.id, symbol_id=symbol.id))
    await db_session.commit()

    resp = await client.post(f"/api/v1/backfill-platform/watchlists/{wl_id}/sync-to-catalog", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["symbol"] == "ENDPOINTSYNC"
    assert body["items"][0]["bars_synced"] == 1
    assert body["items"][0]["instrument_created"] is True

    instruments_resp = await client.get("/api/v1/instruments?q=ENDPOINTSYNC", headers=headers)
    assert len(instruments_resp.json()) == 1
