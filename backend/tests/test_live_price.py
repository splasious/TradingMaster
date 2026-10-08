"""While NSE is open a strategy trades only at a live price
(market_data/live_price.py) -- never a stored close or a price held over
from earlier, which is what 29 Sep's 09:45 entries got."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.market_data import live_price as live_price_module
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext

NOW = datetime(2026, 9, 29, 4, 15, 8, tzinfo=timezone.utc)  # 09:45:08 IST


class FakeKite:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.calls: list[list[str]] = []

    async def get_ltp_batch(self, keys: list[str]) -> dict[str, float]:
        self.calls.append(keys)
        return {k: self.prices[k] for k in keys if k in self.prices}


@pytest.fixture
def market_open(monkeypatch):
    monkeypatch.setattr(live_price_module, "nse_market_open", lambda now: True)

    async def no_wait():
        return None

    monkeypatch.setattr(live_price_module.quote_pacer, "wait", no_wait)


def _kite(monkeypatch, broker) -> None:
    async def fake_broker(db):
        return broker

    monkeypatch.setattr(live_price_module, "kite_broker", fake_broker)


async def _option(db, *, stored_close: float | None = 203.65) -> Instrument:
    inst = Instrument(exchange="NFO", symbol="NIFTY26O0622800CE", name="NIFTY 22800 CE", instrument_type="option",
                      data_source="zerodha_kite", external_ref="NIFTY26O0622800CE")
    db.add(inst)
    await db.flush()
    if stored_close is not None:  # yesterday's 15:15 15m candle
        ts = datetime(2026, 9, 28, 9, 45, tzinfo=timezone.utc)
        db.add(OhlcvCandle(instrument_id=inst.id, timeframe="15m", ts=ts, open=stored_close, high=stored_close,
                           low=stored_close, close=stored_close, source="bf"))
    await db.commit()
    return inst


def _forget(instrument_id) -> None:
    for store in (tick_engine._real_price, tick_engine._real_price_at, tick_engine._real_price_source,
                  tick_engine._last_price, tick_engine._subscriber_counts):
        store.pop(instrument_id, None)


def _ctx(db) -> NativeContext:
    return NativeContext(db=db, portfolio=None, deployment=None, state={}, now=NOW)


async def test_first_read_of_an_untracked_contract_asks_kite(db_session, market_open, monkeypatch):
    inst = await _option(db_session)
    kite = FakeKite({"NFO:NIFTY26O0622800CE": 131.25})
    _kite(monkeypatch, kite)
    try:
        assert await _ctx(db_session).get_price(inst.id) == 131.25  # not yesterday's 203.65
        assert kite.calls == [["NFO:NIFTY26O0622800CE"]]
        assert tick_engine.get_current_price(inst.id) == 131.25
    finally:
        _forget(inst.id)


async def test_a_fresh_live_price_is_used_without_asking_kite(db_session, market_open, monkeypatch):
    inst = await _option(db_session)
    kite = FakeKite({})
    _kite(monkeypatch, kite)
    tick_engine.set_real_price(inst.id, 128.40, source="kite_rest")
    try:
        ctx = NativeContext(db=db_session, portfolio=None, deployment=None, state={}, now=datetime.now(timezone.utc))
        assert await ctx.get_price(inst.id) == 128.40
        assert kite.calls == []
    finally:
        _forget(inst.id)


async def test_a_price_held_over_from_earlier_is_not_live(db_session, market_open, monkeypatch):
    inst = await _option(db_session)
    kite = FakeKite({"NFO:NIFTY26O0622800CE": 131.25})
    _kite(monkeypatch, kite)
    tick_engine.set_real_price(inst.id, 206.60, source="kite_rest")
    tick_engine._real_price_at[inst.id] = NOW - timedelta(hours=18)  # yesterday's last poll
    try:
        assert await _ctx(db_session).get_price(inst.id) == 131.25
    finally:
        _forget(inst.id)


async def test_no_price_rather_than_a_stale_one_when_kite_is_unavailable(db_session, market_open, monkeypatch):
    inst = await _option(db_session)
    _kite(monkeypatch, None)  # Zerodha not logged in
    try:
        assert await _ctx(db_session).get_price(inst.id) is None  # the strategy skips and retries next tick
        assert tick_engine.get_current_price(inst.id) == 203.65  # display still has something to show
    finally:
        _forget(inst.id)


async def test_outside_market_hours_the_stored_close_still_serves(db_session, monkeypatch):
    inst = await _option(db_session)
    _kite(monkeypatch, FakeKite({}))
    try:
        assert await _ctx(db_session).get_price(inst.id) == 203.65
    finally:
        _forget(inst.id)


async def test_a_failed_price_batch_is_asked_for_once_more(db_session, market_open, monkeypatch):
    """8 Oct: one failed Kite request at a 15-minute close left every stock in
    it without a price, and RS Rotation 15 MIN's PCR exit kept two holdings."""
    from app.services.broker.zerodha_broker import KiteAPIError

    inst = await _option(db_session)

    class FlakyKite(FakeKite):
        async def get_ltp_batch(self, keys):
            self.calls.append(keys)
            if len(self.calls) == 1:
                raise KiteAPIError("Too many requests")
            return {k: self.prices[k] for k in keys if k in self.prices}

    kite = FlakyKite({"NFO:NIFTY26O0622800CE": 131.25})
    _kite(monkeypatch, kite)
    try:
        prices = await _ctx(db_session).get_prices([inst.id])
        assert prices == {inst.id: 131.25} and len(kite.calls) == 2
    finally:
        _forget(inst.id)


async def test_a_stock_moved_to_the_be_series_is_priced_from_its_be_quote(db_session, market_open, monkeypatch):
    """NSE moves a stock under surveillance to trade-to-trade, and Kite then
    quotes it only as "<SYMBOL>-BE": on 8 Oct, with the live feed down,
    "NSE:HFCL" got no answer and RS Rotation 15 MIN couldn't sell HFCL."""
    stock = Instrument(exchange="NSE", symbol="HFCL", name="HFCL", instrument_type="equity", data_source="zerodha_kite", external_ref="HFCL")
    plain = Instrument(exchange="NSE", symbol="SBIN", name="SBIN", instrument_type="equity", data_source="zerodha_kite", external_ref="SBIN")
    db_session.add_all([stock, plain])
    await db_session.commit()
    kite = FakeKite({"NSE:HFCL-BE": 269.0, "NSE:SBIN": 800.0})
    _kite(monkeypatch, kite)
    try:
        assert await _ctx(db_session).get_prices([stock.id, plain.id]) == {stock.id: 269.0, plain.id: 800.0}
        assert kite.calls == [["NSE:HFCL", "NSE:SBIN"], ["NSE:HFCL-BE"]]  # asked again as -BE, only for the one with no answer
        _forget(stock.id)
        assert await _ctx(db_session).get_price(stock.id) == 269.0  # the single-price path too
    finally:
        _forget(stock.id)
        _forget(plain.id)
