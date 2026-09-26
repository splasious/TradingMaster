import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfOhlcvBar, BfSettings, BfSymbol
from app.services.backfill_platform.live_sync_scheduler import BfLiveSyncScheduler
from app.services.market_data.hours import nse_market_open

pytestmark = pytest.mark.usefixtures("show_delta")  # Delta Exchange is hidden by default

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


def _delta_rows(*times: datetime) -> list[dict]:
    return [
        {"time": int(t.timestamp()), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}
        for t in times
    ]


def _fake_delta(monkeypatch, rows: list[dict]) -> None:
    async def fake_get(client_self, url, **kwargs):
        body = {"success": True, "result": rows}
        return httpx.Response(200, content=json.dumps(body).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_sync_symbol_leaves_the_minute_in_progress_for_a_later_tick(db_session: AsyncSession, monkeypatch):
    from app.models.market_data import OhlcvCandle
    from app.services.market_data.delta_source import DeltaExchangeDataSource

    symbol = BfSymbol(source="delta", symbol="TSLAXUSD", display_name="Tesla xStock Token")
    db_session.add(symbol)
    await db_session.commit()
    now = datetime(2026, 9, 25, 5, 0, 30, tzinfo=timezone.utc)
    _fake_delta(monkeypatch, _delta_rows(
        datetime(2026, 9, 25, 4, 58, tzinfo=timezone.utc),
        datetime(2026, 9, 25, 4, 59, tzinfo=timezone.utc),
        datetime(2026, 9, 25, 5, 0, tzinfo=timezone.utc),  # still forming at 05:00:30
    ))

    await BfLiveSyncScheduler()._sync_symbol(db_session, symbol, DeltaExchangeDataSource(), "1m", now)

    stored = (await db_session.execute(select(BfOhlcvBar.ts).where(BfOhlcvBar.symbol_id == symbol.id).order_by(BfOhlcvBar.ts))).scalars().all()
    assert [ts.replace(tzinfo=timezone.utc).minute for ts in stored] == [58, 59]
    # Never synced to the main catalog, so it isn't created there by the live sync.
    assert (await db_session.execute(select(OhlcvCandle))).scalars().all() == []


async def test_sync_symbol_copies_the_new_minutes_to_the_chart_table(db_session: AsyncSession, monkeypatch):
    from app.models.market_data import OhlcvCandle
    from app.services.market_data.delta_source import DeltaExchangeDataSource

    synced_at = datetime(2026, 9, 24, tzinfo=timezone.utc)
    symbol = BfSymbol(source="delta", symbol="AAPLXUSD", display_name="Apple xStock Token", last_synced_at=synced_at)
    db_session.add(symbol)
    await db_session.flush()
    # A 5m bar a backfill saved but the catalog sync hasn't copied yet --
    # left for CatalogSyncScheduler, which last_synced_at still points to.
    db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="5m", ts=datetime(2026, 9, 25, 4, 55, tzinfo=timezone.utc),
                              open=1, high=1, low=1, close=1, volume=1))
    await db_session.commit()
    now = datetime(2026, 9, 25, 5, 0, 30, tzinfo=timezone.utc)
    _fake_delta(monkeypatch, _delta_rows(
        datetime(2026, 9, 25, 4, 58, tzinfo=timezone.utc), datetime(2026, 9, 25, 4, 59, tzinfo=timezone.utc),
    ))

    await BfLiveSyncScheduler()._sync_symbol(db_session, symbol, DeltaExchangeDataSource(), "1m", now)

    candles = (await db_session.execute(select(OhlcvCandle).order_by(OhlcvCandle.ts))).scalars().all()
    assert [(c.timeframe, c.ts.replace(tzinfo=timezone.utc).minute, c.close) for c in candles] == [("1m", 58, 100.5), ("1m", 59, 100.5)]
    await db_session.refresh(symbol)
    assert symbol.last_synced_at.replace(tzinfo=timezone.utc) == synced_at


async def test_one_symbol_failing_does_not_affect_the_others_in_a_tick(db_engine, db_session: AsyncSession, monkeypatch):
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.backfill_platform import live_sync_scheduler

    first = BfSymbol(source="delta", symbol="AAAXUSD", display_name="A")
    second = BfSymbol(source="delta", symbol="BBBXUSD", display_name="B")
    db_session.add_all([first, second])
    await db_session.commit()
    finished = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    _fake_delta(monkeypatch, _delta_rows(finished))
    real_save = live_sync_scheduler.save_bars

    async def failing_for_first(db, symbol_id, timeframe, bars):
        result = await real_save(db, symbol_id, timeframe, bars)  # writes, then fails before commit
        if symbol_id == first.id:
            raise RuntimeError("simulated failure")
        return result

    monkeypatch.setattr(live_sync_scheduler, "save_bars", failing_for_first)
    monkeypatch.setattr(live_sync_scheduler, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))
    db_session.add(BfSettings(id=1, delta_enabled=True))
    await db_session.commit()

    assert await BfLiveSyncScheduler()._sync_once() == 1

    counts = {}
    for symbol in (first, second):
        rows = (await db_session.execute(select(BfOhlcvBar).where(BfOhlcvBar.symbol_id == symbol.id))).scalars().all()
        counts[symbol.symbol] = len(rows)
    assert counts == {"AAAXUSD": 0, "BBBXUSD": 1}  # the failed symbol's write was rolled back, not committed with the next


async def test_nothing_is_fetched_while_delta_is_paused(db_engine, db_session: AsyncSession, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.backfill_platform import live_sync_scheduler

    db_session.add(BfSymbol(source="delta", symbol="PAUSEDXUSD", display_name="Paused"))
    db_session.add(BfSettings(id=1, delta_enabled=False))  # the default: paused on the Data Backfill page
    await db_session.commit()

    async def no_calls(client_self, url, **kwargs):
        raise AssertionError("Delta must not be called while paused")

    monkeypatch.setattr(httpx.AsyncClient, "get", no_calls)
    monkeypatch.setattr(live_sync_scheduler, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))

    assert await BfLiveSyncScheduler()._sync_once() == 0
    assert (await db_session.execute(select(BfOhlcvBar))).scalars().all() == []
