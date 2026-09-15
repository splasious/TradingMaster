import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker
from app.services.market_data import kite_rest_price_feed as feed_module
from app.services.market_data.kite_rest_price_feed import KiteRestPriceFeed
from app.services.market_data.tick_engine import TickEngine
from tests.test_kite_ticker_service import _seed_connected_account, db_session_cm


async def test_refresh_returns_zero_without_a_connected_account(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(feed_module, "AsyncSessionLocal", lambda: db_session_cm(db_session))

    instrument = Instrument(
        exchange="NSE", symbol="INFY", name="Infosys", instrument_type="equity",
        data_source="zerodha_kite", external_ref="INFY",
    )
    db_session.add(instrument)
    await db_session.commit()

    engine = TickEngine()
    engine.subscribe(instrument.id, seed_price=100.0)
    feed = KiteRestPriceFeed(engine)

    updated = await feed.refresh_active_instruments()
    assert updated == 0
    assert engine.get_current_price(instrument.id) == 100.0  # untouched


async def test_refresh_updates_only_subscribed_zerodha_instruments(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(feed_module, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    await _seed_connected_account(db_session, connected=True, access_token="tok_a")

    subscribed = Instrument(
        exchange="NSE", symbol="INFY", name="Infosys", instrument_type="equity",
        data_source="zerodha_kite", external_ref="INFY",
    )
    not_subscribed = Instrument(
        exchange="NSE", symbol="TCS", name="TCS", instrument_type="equity",
        data_source="zerodha_kite", external_ref="TCS",
    )
    delta_subscribed = Instrument(
        exchange="DELTA", symbol="BTCUSD", name="Bitcoin", instrument_type="perpetual_future",
        data_source="delta_exchange", external_ref="BTCUSD",
    )
    db_session.add_all([subscribed, not_subscribed, delta_subscribed])
    await db_session.commit()

    engine = TickEngine()
    engine.subscribe(subscribed.id, seed_price=1.0)
    engine.subscribe(delta_subscribed.id, seed_price=1.0)  # subscribed, but not zerodha_kite -- must be ignored here

    async def fake_get_ltp_batch(self, instruments):
        assert instruments == ["NSE:INFY"]
        return {"NSE:INFY": 1502.35}

    monkeypatch.setattr(ZerodhaKiteBroker, "get_ltp_batch", fake_get_ltp_batch)

    feed = KiteRestPriceFeed(engine)
    updated = await feed.refresh_active_instruments()

    assert updated == 1
    assert engine.get_current_price(subscribed.id) == 1502.35
    assert engine.get_current_price(not_subscribed.id) is None
    assert engine.get_current_price(delta_subscribed.id) == 1.0  # untouched by this feed


async def test_refresh_chunks_into_batches_at_batch_size(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(feed_module, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    monkeypatch.setattr(feed_module, "BATCH_SIZE", 3)
    await _seed_connected_account(db_session, connected=True, access_token="tok_a")

    engine = TickEngine()
    instruments = []
    for i in range(7):
        instrument = Instrument(
            exchange="NSE", symbol=f"SYM{i}", name=f"Sym {i}", instrument_type="equity",
            data_source="zerodha_kite", external_ref=f"SYM{i}",
        )
        db_session.add(instrument)
        instruments.append(instrument)
    await db_session.commit()
    for instrument in instruments:
        engine.subscribe(instrument.id, seed_price=1.0)

    calls: list[list[str]] = []

    async def fake_get_ltp_batch(self, batch):
        calls.append(batch)
        return {key: 42.0 for key in batch}

    monkeypatch.setattr(ZerodhaKiteBroker, "get_ltp_batch", fake_get_ltp_batch)

    feed = KiteRestPriceFeed(engine)
    updated = await feed.refresh_active_instruments()

    assert updated == 7
    assert len(calls) == 3  # ceil(7/3)
    assert all(len(batch) <= 3 for batch in calls)
    assert all(engine.get_current_price(i.id) == 42.0 for i in instruments)


async def test_refresh_continues_past_one_failed_batch(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(feed_module, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    monkeypatch.setattr(feed_module, "BATCH_SIZE", 1)
    await _seed_connected_account(db_session, connected=True, access_token="tok_a")

    good = Instrument(exchange="NSE", symbol="GOOD", name="Good", instrument_type="equity", data_source="zerodha_kite", external_ref="GOOD")
    bad = Instrument(exchange="NSE", symbol="BAD", name="Bad", instrument_type="equity", data_source="zerodha_kite", external_ref="BAD")
    db_session.add_all([good, bad])
    await db_session.commit()

    engine = TickEngine()
    engine.subscribe(good.id, seed_price=1.0)
    engine.subscribe(bad.id, seed_price=1.0)

    async def fake_get_ltp_batch(self, batch):
        if batch == ["NSE:BAD"]:
            raise KiteAPIError("boom")
        return {"NSE:GOOD": 55.0}

    monkeypatch.setattr(ZerodhaKiteBroker, "get_ltp_batch", fake_get_ltp_batch)

    feed = KiteRestPriceFeed(engine)
    updated = await feed.refresh_active_instruments()

    assert updated == 1
    assert engine.get_current_price(good.id) == 55.0
    assert engine.get_current_price(bad.id) == 1.0  # untouched, but the good one still went through


async def test_refresh_returns_zero_with_no_subscribers_at_all(db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(feed_module, "AsyncSessionLocal", lambda: db_session_cm(db_session))
    engine = TickEngine()
    feed = KiteRestPriceFeed(engine)
    assert await feed.refresh_active_instruments() == 0
