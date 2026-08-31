import asyncio
import json
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.market_data.real_price_feed import RealPriceFeed, fetch_real_price
from app.services.market_data.tick_engine import TickEngine


def test_tick_engine_serves_real_price_flat_once_set():
    engine = TickEngine()
    iid = uuid.uuid4()
    engine.subscribe(iid, seed_price=100.0)
    engine.set_real_price(iid, 1234.5, "delta")

    msg1 = engine._next_tick_message(iid, "t1")
    msg2 = engine._next_tick_message(iid, "t2")

    assert msg1["price"] == 1234.5
    assert msg1["source"] == "delta"
    assert msg2["price"] == 1234.5  # held flat, not randomly perturbed
    assert msg2["source"] == "delta"


def test_tick_engine_falls_back_to_simulated_without_real_price():
    engine = TickEngine()
    iid = uuid.uuid4()
    engine.subscribe(iid, seed_price=100.0)

    msg = engine._next_tick_message(iid, "t1")

    assert msg["source"] == "simulated"


def test_get_current_price_prefers_real_over_simulated():
    engine = TickEngine()
    iid = uuid.uuid4()
    engine.subscribe(iid, seed_price=50.0)
    assert engine.get_current_price(iid) == 50.0

    engine.set_real_price(iid, 999.0, "yahoo")
    assert engine.get_current_price(iid) == 999.0


async def test_fetch_real_price_delta_uses_real_ticker(monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        payload = {"success": True, "result": {"close": "42.5", "product_id": 1, "mark_price": "42.6"}}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    instrument = Instrument(
        id=uuid.uuid4(), exchange="DELTA", symbol="NVDAXUSD", name="NVIDIA xStock Token",
        instrument_type="perpetual_future", data_source="delta_exchange", external_ref="NVDAXUSD",
    )
    result = await fetch_real_price(instrument)
    assert result == (42.5, "delta")


async def test_fetch_real_price_yahoo_returns_none_outside_market_hours():
    instrument = Instrument(
        id=uuid.uuid4(), exchange="NSE", symbol="RELIANCE", name="Reliance",
        instrument_type="equity", data_source="yahoo_nse", external_ref="RELIANCE",
    )
    result = await fetch_real_price(instrument, market_open=False)
    assert result is None


async def test_fetch_real_price_yahoo_returns_last_close_during_market_hours(monkeypatch):
    from app.services.market_data import yahoo_source as yahoo_module

    async def fake_get_historical_data(self, external_ref, timeframe, start, end):
        return [{"ts": datetime.now(timezone.utc), "open": 1, "high": 1, "low": 1, "close": 555.0, "volume": 10}]

    monkeypatch.setattr(yahoo_module.YahooNSEDataSource, "get_historical_data", fake_get_historical_data)

    instrument = Instrument(
        id=uuid.uuid4(), exchange="NSE", symbol="RELIANCE", name="Reliance",
        instrument_type="equity", data_source="yahoo_nse", external_ref="RELIANCE",
    )
    result = await fetch_real_price(instrument, market_open=True)
    assert result == (555.0, "yahoo")


async def test_fetch_real_price_unmapped_source_returns_none():
    instrument = Instrument(
        id=uuid.uuid4(), exchange="NSE", symbol="X", name="X",
        instrument_type="equity", data_source="zerodha_kite", external_ref="X",
    )
    result = await fetch_real_price(instrument)
    assert result is None


async def test_refresh_active_instruments_updates_only_subscribed(db_session: AsyncSession, monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        payload = {"success": True, "result": {"close": "77.0", "product_id": 1, "mark_price": "77.0"}}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    subscribed = Instrument(
        exchange="DELTA", symbol="PLTRBUSD", name="Palantir bStocks Token",
        instrument_type="perpetual_future", data_source="delta_exchange", external_ref="PLTRBUSD",
    )
    not_subscribed = Instrument(
        exchange="DELTA", symbol="AAPLXUSD", name="Apple xStock Token",
        instrument_type="perpetual_future", data_source="delta_exchange", external_ref="AAPLXUSD",
    )
    db_session.add_all([subscribed, not_subscribed])
    await db_session.commit()

    engine = TickEngine()
    engine.subscribe(subscribed.id, seed_price=1.0)

    from app.services import market_data as market_data_pkg  # noqa: F401
    import app.services.market_data.real_price_feed as feed_module

    monkeypatch.setattr(feed_module, "AsyncSessionLocal", lambda: db_session_cm(db_session))

    feed = RealPriceFeed(engine)
    updated = await feed.refresh_active_instruments()

    assert updated == 1
    assert engine.get_current_price(subscribed.id) == 77.0
    assert engine.get_current_price(not_subscribed.id) is None


async def test_refresh_active_instruments_runs_fetches_concurrently(db_session: AsyncSession, monkeypatch):
    # Regression test for the real bug: hundreds of subscribed instruments
    # each doing a real HTTP round-trip, sequentially, could take far
    # longer than one refresh interval to get all the way through --
    # starving newly-subscribed instruments of any real update for a long
    # time (verified live: RELIANCE's fetch worked instantly on its own,
    # but never got a turn amid 231 other subscribed instruments). This
    # asserts a batch of slow fetches completes in roughly one fetch's
    # duration, not duration * count.
    import time

    FETCH_DELAY = 0.2
    N = 20

    async def fake_get(client_self, url, **kwargs):
        await asyncio.sleep(FETCH_DELAY)
        payload = {"success": True, "result": {"close": "42.0", "product_id": 1, "mark_price": "42.0"}}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    engine = TickEngine()
    instruments = []
    for i in range(N):
        instrument = Instrument(
            exchange="DELTA", symbol=f"CONC{i}USD", name=f"Concurrency Test {i}",
            instrument_type="perpetual_future", data_source="delta_exchange", external_ref=f"CONC{i}USD",
        )
        db_session.add(instrument)
        instruments.append(instrument)
    await db_session.commit()
    for instrument in instruments:
        engine.subscribe(instrument.id, seed_price=1.0)

    import app.services.market_data.real_price_feed as feed_module

    monkeypatch.setattr(feed_module, "AsyncSessionLocal", lambda: db_session_cm(db_session))

    feed = RealPriceFeed(engine)
    start = time.monotonic()
    updated = await feed.refresh_active_instruments()
    elapsed = time.monotonic() - start

    assert updated == N
    # Sequential would take N * FETCH_DELAY = 4.0s; concurrent should be
    # close to one FETCH_DELAY plus overhead -- well under half the
    # sequential time either way.
    assert elapsed < (N * FETCH_DELAY) / 2


class db_session_cm:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc) -> None:
        return None
