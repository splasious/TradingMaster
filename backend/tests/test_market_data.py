import inspect
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import BackfillJob, BackfillStatus, OhlcvCandle
from app.services.market_data import backfill as backfill_module
from app.services.market_data.base import Bar, MarketDataSource, MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.market_data.registry import get_market_data_source
from app.services.market_data.validation import compute_quality
from app.services.market_data.yahoo_source import YahooNSEDataSource


def test_adapters_implement_full_interface():
    interface_methods = {name for name, _ in inspect.getmembers(MarketDataSource, predicate=inspect.isfunction)}
    for adapter in (YahooNSEDataSource(), DeltaExchangeDataSource()):
        for name in interface_methods:
            assert hasattr(adapter, name)
            assert inspect.iscoroutinefunction(getattr(adapter, name))


def test_registry_resolves_known_sources():
    assert isinstance(get_market_data_source("yahoo_nse"), YahooNSEDataSource)
    assert isinstance(get_market_data_source("delta_exchange"), DeltaExchangeDataSource)


def test_registry_rejects_unknown_source():
    with pytest.raises(ValueError):
        get_market_data_source("nonexistent")


async def test_delta_source_rejects_unsupported_timeframe():
    with pytest.raises(MarketDataSourceError):
        await DeltaExchangeDataSource().get_historical_data("BTCUSD", "1mo", None, None)


async def test_delta_get_ticker_parses_price_and_product_id(monkeypatch):
    async def fake_get(self, url, **kwargs):
        payload = {"success": True, "result": {"close": 77637.5, "mark_price": "77637.99025811", "product_id": 27}}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    ticker = await DeltaExchangeDataSource().get_ticker("BTCUSD")
    assert ticker == {"price": 77637.5, "product_id": 27, "mark_price": pytest.approx(77637.99025811)}


async def test_delta_get_ticker_raises_on_api_error(monkeypatch):
    async def fake_get(self, url, **kwargs):
        payload = {"success": False, "error": {"code": "invalid_symbol"}}
        return httpx.Response(400, content=json.dumps(payload).encode(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    with pytest.raises(MarketDataSourceError):
        await DeltaExchangeDataSource().get_ticker("NOTREAL")


def _candle(ts: datetime, o: float, h: float, l: float, c: float) -> OhlcvCandle:
    return OhlcvCandle(instrument_id=uuid.uuid4(), timeframe="1d", ts=ts, open=o, high=h, low=l, close=c, source="test")


def test_quality_report_perfect_data():
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)  # a Monday
    candles = [_candle(base + timedelta(days=i), 100, 105, 99, 102) for i in range(5)]  # Mon-Fri
    report = compute_quality(candles, "1d")
    assert report["quality_score"] == 100.0
    assert report["invalid_ohlc_count"] == 0
    assert report["missing_weekday_gaps"] == 0


def test_quality_report_detects_invalid_ohlc_and_bad_prices():
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    candles = [
        _candle(base, 100, 105, 99, 102),
        _candle(base + timedelta(days=1), 100, 90, 99, 102),  # high < close: invalid
        _candle(base + timedelta(days=2), -5, 105, 99, 102),  # non-positive open
    ]
    report = compute_quality(candles, "1d")
    assert report["invalid_ohlc_count"] == 1
    assert report["non_positive_price_count"] == 1
    assert report["quality_score"] < 100.0


def test_quality_report_detects_missing_weekday_gap():
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday
    candles = [
        _candle(base, 100, 105, 99, 102),
        _candle(base + timedelta(days=3), 100, 105, 99, 102),  # skipped Tue/Wed
    ]
    report = compute_quality(candles, "1d")
    assert report["missing_weekday_gaps"] == 2


def test_quality_report_empty():
    report = compute_quality([], "1d")
    assert report["candle_count"] == 0
    assert report["quality_score"] == 0.0


class _StubSource(MarketDataSource):
    def __init__(self, bars: list[Bar]) -> None:
        self.bars = bars

    async def get_historical_data(self, external_ref, timeframe, start, end) -> list[Bar]:
        return self.bars


async def test_backfill_job_inserts_candles_and_detects_duplicates_on_rerun(db_session: AsyncSession, monkeypatch):
    instrument = Instrument(
        exchange="NSE", symbol="TESTCO", name="Test Co", instrument_type="equity",
        data_source="yahoo_nse", external_ref="TESTCO",
    )
    db_session.add(instrument)
    await db_session.commit()

    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    bars: list[Bar] = [
        Bar(ts=base + timedelta(days=i), open=100 + i, high=105 + i, low=99 + i, close=102 + i, volume=1000.0)
        for i in range(3)
    ]
    monkeypatch.setattr(backfill_module, "get_market_data_source", lambda source: _StubSource(bars))

    job = BackfillJob(instrument_id=instrument.id, timeframe="1d")
    db_session.add(job)
    await db_session.commit()

    # run_backfill_job opens its own session via AsyncSessionLocal; patch it
    # to hand back this test's session, wrapped so `async with` doesn't
    # close it on exit (the test needs it to make assertions afterward).
    class _NoCloseSession:
        def __init__(self, session):
            self._session = session

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(backfill_module, "AsyncSessionLocal", lambda: _NoCloseSession(db_session))

    await backfill_module.run_backfill_job(job.id)

    await db_session.refresh(job)
    assert job.status == BackfillStatus.COMPLETED.value
    assert job.downloaded_count == 3
    assert job.inserted_count == 3
    assert job.duplicate_count == 0

    candle_count = len((await db_session.execute(select(OhlcvCandle))).scalars().all())
    assert candle_count == 3

    # Re-running with the same bars must not create duplicate rows.
    job2 = BackfillJob(instrument_id=instrument.id, timeframe="1d")
    db_session.add(job2)
    await db_session.commit()
    await backfill_module.run_backfill_job(job2.id)
    await db_session.refresh(job2)

    assert job2.inserted_count == 0
    assert job2.duplicate_count == 3
    candle_count_after = len((await db_session.execute(select(OhlcvCandle))).scalars().all())
    assert candle_count_after == 3


async def test_instruments_endpoint_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/instruments")
    assert resp.status_code == 401


async def test_quotes_returns_latest_daily_bar_per_instrument(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = Instrument(
        exchange="NSE", symbol="QUOTETEST", name="Quote Test Co", instrument_type="equity",
        data_source="yahoo_nse", external_ref="QUOTETEST",
    )
    db_session.add(instrument)
    await db_session.flush()
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    db_session.add_all(
        [
            OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base, open=100, high=101, low=99, close=100.5, volume=1000, source="yahoo_nse"),
            OhlcvCandle(
                instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=1),
                open=101, high=105, low=100, close=104.0, volume=2500, source="yahoo_nse",
            ),
        ]
    )
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login.json()["access_token"]

    resp = await client.post(
        "/api/v1/market-data/quotes",
        json={"instrument_ids": [str(instrument.id)]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["instrument_id"] == str(instrument.id)
    assert body[0]["prev_close"] == 104.0  # the later of the two bars, not the earlier
    assert body[0]["volume"] == 2500


async def test_quotes_empty_ids_returns_empty_list(client: AsyncClient, seeded_admin: dict):
    login = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login.json()["access_token"]
    resp = await client.post(
        "/api/v1/market-data/quotes", json={"instrument_ids": []}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


async def test_backfill_requires_trader_or_admin_role(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = Instrument(
        exchange="NSE", symbol="TESTCO2", name="Test Co 2", instrument_type="equity",
        data_source="yahoo_nse", external_ref="TESTCO2",
    )
    db_session.add(instrument)
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login.json()["access_token"]

    resp = await client.post(
        "/api/v1/market-data/backfill",
        json={"instrument_id": str(instrument.id), "timeframe": "1d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["instrument_id"] == str(instrument.id)
