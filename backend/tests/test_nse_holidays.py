from datetime import date

import httpx
import pytest

from app.services.market_data import nse_holidays


@pytest.fixture(autouse=True)
def _reset_live_holidays():
    """_live_holidays is process-wide, mutable state -- a refresh in one
    test must not leak into the next."""
    nse_holidays._live_holidays.clear()
    nse_holidays.last_live_fetch_at = None
    nse_holidays.last_live_fetch_error = None
    yield
    nse_holidays._live_holidays.clear()
    nse_holidays.last_live_fetch_at = None
    nse_holidays.last_live_fetch_error = None


def test_is_trading_holiday_true_for_a_seeded_static_date():
    assert nse_holidays.is_trading_holiday(date(2026, 10, 2)) is True  # Gandhi Jayanti


def test_is_trading_holiday_false_for_an_ordinary_trading_day():
    assert nse_holidays.is_trading_holiday(date(2026, 9, 10)) is False


def test_is_trading_holiday_false_for_an_unseeded_year():
    assert nse_holidays.is_trading_holiday(date(2030, 10, 2)) is False


async def test_refresh_from_nse_parses_fo_rows_and_overrides_static(monkeypatch):
    payload = {"FO": [{"tradingDate": "26-Jan-2026", "description": "Republic Day"}, {"tradingDate": "15-Aug-2026", "description": "Independence Day"}]}

    async def fake_get(self, url, *args, **kwargs):
        if "holiday-master" in url:
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    count = await nse_holidays.refresh_from_nse()
    assert count == 2
    assert nse_holidays.is_trading_holiday(date(2026, 8, 15)) is True  # not in STATIC_HOLIDAYS -- only the live fetch
    assert nse_holidays.last_live_fetch_error is None
    assert nse_holidays.last_live_fetch_at is not None


async def test_refresh_from_nse_falls_back_silently_on_network_failure(monkeypatch):
    async def fake_get(self, url, *args, **kwargs):
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    count = await nse_holidays.refresh_from_nse()
    assert count == 0
    assert nse_holidays.last_live_fetch_error is not None
    # The static seed must still answer correctly -- a blocked live fetch
    # must never make a real holiday invisible.
    assert nse_holidays.is_trading_holiday(date(2026, 10, 2)) is True


async def test_refresh_from_nse_records_error_on_non_200(monkeypatch):
    async def fake_get(self, url, *args, **kwargs):
        return httpx.Response(403, text="blocked", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    count = await nse_holidays.refresh_from_nse()
    assert count == 0
    assert "403" in nse_holidays.last_live_fetch_error


async def test_refresh_from_nse_records_error_on_unrecognized_payload(monkeypatch):
    async def fake_get(self, url, *args, **kwargs):
        return httpx.Response(200, json={"unexpected": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    count = await nse_holidays.refresh_from_nse()
    assert count == 0
    assert nse_holidays.last_live_fetch_error is not None
