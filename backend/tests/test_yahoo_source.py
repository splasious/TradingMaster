import json
from datetime import datetime, timezone

import httpx

from app.services.market_data.yahoo_source import YahooNSEDataSource


async def test_get_historical_data_normalizes_daily_ts_to_midnight_utc(monkeypatch):
    # Regression test for a real bug: the nse-yahoo-data service doesn't
    # always return the same time-of-day for a given trading day's daily
    # bar (depends on yfinance's tz handling at ingest time upstream), so
    # two independent TradingMaster ingestion paths hitting it could each
    # store a different ts for the same calendar day -- two candles for one
    # day. Both a midnight-UTC and an intraday-UTC timestamp for the same
    # calendar date must normalize to the identical stored ts.
    async def fake_get(client_self, url, **kwargs):
        payload = [
            {"ts": "2026-08-13T00:00:00+00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ]
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    source = YahooNSEDataSource(base_url="http://fake")
    bars = await source.get_historical_data("RELIANCE", "1d", None, None)

    assert bars[0]["ts"] == datetime(2026, 8, 13, tzinfo=timezone.utc)


async def test_get_historical_data_normalizes_regardless_of_source_time_of_day(monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        payload = [
            {"ts": "2026-08-13T04:00:00+00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ]
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    source = YahooNSEDataSource(base_url="http://fake")
    bars = await source.get_historical_data("RELIANCE", "1d", None, None)

    # Same calendar day as the midnight-UTC case above -- must produce the
    # identical ts so both ingestion paths dedupe against each other.
    assert bars[0]["ts"] == datetime(2026, 8, 13, tzinfo=timezone.utc)


async def test_get_historical_data_leaves_intraday_ts_untouched(monkeypatch):
    async def fake_get(client_self, url, **kwargs):
        payload = [
            {"ts": "2026-08-13T05:45:00+00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ]
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    source = YahooNSEDataSource(base_url="http://fake")
    bars = await source.get_historical_data("RELIANCE", "5m", None, None)

    assert bars[0]["ts"] == datetime(2026, 8, 13, 5, 45, tzinfo=timezone.utc)
