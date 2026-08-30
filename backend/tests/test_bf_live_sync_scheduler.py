import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.services.backfill_platform.live_sync_scheduler import BfLiveSyncScheduler, nse_market_open

_original_get = httpx.AsyncClient.get


def test_nse_market_open_during_session():
    # 2024-01-01 is a Monday, 10:00 IST = 04:30 UTC
    dt = datetime(2024, 1, 1, 4, 30, tzinfo=timezone.utc)
    assert nse_market_open(dt) is True


def test_nse_market_closed_outside_session():
    dt = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)  # 17:30 IST, after close
    assert nse_market_open(dt) is False


def test_nse_market_closed_on_weekend():
    # 2024-01-06 is a Saturday
    dt = datetime(2024, 1, 6, 4, 30, tzinfo=timezone.utc)
    assert nse_market_open(dt) is False


async def test_sync_once_polls_delta_symbols_and_inserts_bars(db_session: AsyncSession, monkeypatch):
    symbol = BfSymbol(source="delta", symbol="NVDAXUSD", display_name="NVIDIA xStock Token")
    db_session.add(symbol)
    await db_session.commit()

    async def fake_get(client_self, url, **kwargs):
        payload = {"success": True, "result": [
            {"time": int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0},
        ]}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    # Directly exercise _sync_symbol against this test's own session rather
    # than routing through the scheduler's full loop + AsyncSessionLocal.
    scheduler = BfLiveSyncScheduler()
    from app.services.market_data.delta_source import DeltaExchangeDataSource

    await scheduler._sync_symbol(db_session, symbol, DeltaExchangeDataSource(), "1m", datetime.now(timezone.utc))

    bars = (await db_session.execute(select(BfOhlcvBar).where(BfOhlcvBar.symbol_id == symbol.id))).scalars().all()
    assert len(bars) == 1
    assert bars[0].close == 100.5


async def test_sync_symbol_does_not_duplicate_existing_bars(db_session: AsyncSession, monkeypatch):
    symbol = BfSymbol(source="delta", symbol="PLTRBUSD", display_name="Palantir bStocks Token")
    db_session.add(symbol)
    await db_session.flush()
    existing_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="1m", ts=existing_ts, open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()

    async def fake_get(client_self, url, **kwargs):
        payload = {"success": True, "result": [
            {"time": int(existing_ts.timestamp()), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0},
        ]}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    from app.services.market_data.delta_source import DeltaExchangeDataSource

    scheduler = BfLiveSyncScheduler()
    await scheduler._sync_symbol(db_session, symbol, DeltaExchangeDataSource(), "1m", datetime.now(timezone.utc))

    bars = (await db_session.execute(select(BfOhlcvBar).where(BfOhlcvBar.symbol_id == symbol.id))).scalars().all()
    assert len(bars) == 1  # still just the one -- not duplicated
