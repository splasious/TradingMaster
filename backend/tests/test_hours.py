from datetime import datetime, timezone

import pytest

from app.services.market_data import nse_holidays
from app.services.market_data.hours import nse_market_open


@pytest.fixture(autouse=True)
def _reset_live_holidays():
    nse_holidays._live_holidays.clear()
    yield
    nse_holidays._live_holidays.clear()


def test_market_closed_on_a_seeded_static_holiday():
    # 2026-10-02 is a Friday (Gandhi Jayanti) -- an ordinary trading
    # weekday if not for the holiday calendar.
    dt = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)  # 10:30 IST
    assert nse_market_open(dt) is False


def test_market_open_the_day_before_and_after_a_holiday():
    day_before = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)  # Thursday
    day_after = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)  # Monday
    assert nse_market_open(day_before) is True
    assert nse_market_open(day_after) is True


def test_market_closed_on_a_live_fetched_holiday_not_in_the_static_seed():
    # Independence Day 2026 (15 Aug, a Saturday) isn't in STATIC_HOLIDAYS
    # at all -- it's already a weekend. Use a live override on an
    # otherwise-ordinary weekday to prove the live layer, not the weekend
    # check, is what closes it.
    from datetime import date
    nse_holidays._live_holidays[2026] = frozenset({date(2026, 9, 16)})  # a Wednesday

    dt = datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc)  # 10:30 IST
    assert nse_market_open(dt) is False


def test_market_open_on_a_weekday_the_live_calendar_no_longer_flags():
    """A live-fetched year fully overrides the static one for that year --
    if NSE's own calendar for 2026 (once live) doesn't carry a date the
    static seed guessed wrong, the live version wins."""
    from datetime import date
    nse_holidays._live_holidays[2026] = frozenset({date(2026, 12, 25)})  # only Christmas, live

    dt = datetime(2026, 10, 2, 5, 0, tzinfo=timezone.utc)  # Gandhi Jayanti -- in STATIC_HOLIDAYS, not in this live set
    assert nse_market_open(dt) is True
