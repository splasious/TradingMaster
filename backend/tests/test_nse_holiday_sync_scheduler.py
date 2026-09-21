import httpx
import pytest

from app.services.market_data import nse_holidays
from app.services.market_data.nse_holiday_sync_scheduler import NseHolidaySyncScheduler


@pytest.fixture(autouse=True)
def _reset_live_holidays():
    nse_holidays._live_holidays.clear()
    nse_holidays.last_live_fetch_at = None
    nse_holidays.last_live_fetch_error = None
    yield
    nse_holidays._live_holidays.clear()
    nse_holidays.last_live_fetch_at = None
    nse_holidays.last_live_fetch_error = None


async def test_tick_records_synced_count_and_run_time(monkeypatch):
    payload = {"FO": [{"tradingDate": "26-Jan-2026", "description": "Republic Day"}]}

    async def fake_get(self, url, *args, **kwargs):
        if "holiday-master" in url:
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        return httpx.Response(200, text="ok", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    scheduler = NseHolidaySyncScheduler()
    assert scheduler.last_run_at is None
    await scheduler._tick()

    assert scheduler.last_synced_count == 1
    assert scheduler.last_run_at is not None


async def test_tick_does_not_raise_when_nse_is_unreachable(monkeypatch):
    async def fake_get(self, url, *args, **kwargs):
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    scheduler = NseHolidaySyncScheduler()
    await scheduler._tick()  # must not raise

    assert scheduler.last_synced_count == 0
    assert scheduler.last_run_at is not None
