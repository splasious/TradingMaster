"""NIFTY's recent candles for the Telegram pictures (notifications/candle_panel.py,
21 Oct): where a time falls on the chart, the forming candle built from the
live price, and time labels that don't collide where a session changes."""

from datetime import datetime, timedelta, timezone

from app.services.broker.zerodha_broker import IST
from app.services.notifications import candle_panel
from app.services.notifications.candle_panel import Recent, with_ma


def ist(day: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=IST)


def utc(at: datetime) -> datetime:
    return at.astimezone(timezone.utc)


def candles(day: int, hh: int, mm: int, count: int, close: float = 25000.0) -> list[dict]:
    start = ist(day, hh, mm)
    return [{"ts": utc(start + timedelta(minutes=15 * n)), "open": close, "high": close + 5, "low": close - 5, "close": close}
            for n in range(count)]


def test_the_candle_forming_now_starts_on_the_15_minute_grid_from_0915():
    assert candle_panel.period_start(utc(ist(21, 9, 14, 59))) is None
    assert candle_panel.period_start(utc(ist(21, 9, 15))) == utc(ist(21, 9, 15))
    assert candle_panel.period_start(utc(ist(21, 11, 37, 20))) == utc(ist(21, 11, 30))
    assert candle_panel.period_start(utc(ist(21, 15, 29))) == utc(ist(21, 15, 15))
    assert candle_panel.period_start(utc(ist(21, 15, 30))) is None


def test_the_forming_candle_opens_at_the_last_close_only_when_it_follows_it():
    today = candles(21, 11, 0, 2, close=25100.0)  # 11:00 and 11:15, finished
    follows = candle_panel._forming(today, utc(ist(21, 11, 31)), 25120.0)
    assert (follows["ts"], follows["open"], follows["low"], follows["high"], follows["close"]) == (
        utc(ist(21, 11, 30)), 25100.0, 25100.0, 25120.0, 25120.0)
    after_a_gap = candle_panel._forming(today[:1], utc(ist(21, 11, 31)), 25120.0)  # 11:15 not saved yet
    assert after_a_gap["open"] == 25120.0
    at_the_open = candle_panel._forming(candles(20, 15, 15, 1), utc(ist(21, 9, 15, 20)), 25060.0)  # yesterday's last
    assert at_the_open["open"] == 25060.0 and at_the_open["ts"] == utc(ist(21, 9, 15))
    assert candle_panel._forming(today, utc(ist(21, 15, 31)), 25120.0) is None  # the session is over


def test_times_on_the_chart_map_to_candles_side_by_side():
    bars = candles(20, 14, 45, 3) + candles(21, 9, 15, 2)  # 14:45, 15:00, 15:15 | 09:15, 09:30
    recent = Recent(bars, with_ma(bars))
    assert recent.x(ist(20, 14, 0)) == -0.5  # before the chart
    assert recent.x(ist(20, 15, 7, 30)) == 1.5
    assert recent.x(ist(21, 8, 0)) == 3.0  # overnight: the next session's first candle
    assert recent.x(ist(21, 9, 45)) == 5.0  # the newest candle's end
    assert recent.describe() == "last 5 candles"


def test_time_labels_skip_the_one_just_before_a_session_opens_and_carry_the_dates():
    bars = candles(16, 13, 15, 9) + candles(19, 9, 15, 13)  # 16 Oct 13:15 .. 15:15, 19 Oct 09:15 .. 12:15
    forming = candles(19, 12, 30, 1)[0]
    recent = Recent(bars, with_ma(bars), forming)
    assert candle_panel.session_dividers(recent) == [(9, ist(19, 9, 15).date())]
    ticks, labels = candle_panel.time_ticks(recent)
    assert labels == ["13:15\n16 Oct", "09:15\n19 Oct", "11:15", "12:30"]  # not 15:15, right before 09:15
    assert ticks == [0.5, 9.5, 17.5, 22.5]


def test_one_session_needs_no_dates_and_a_late_start_still_gets_a_time():
    one_day = candles(19, 9, 15, 12)
    assert candle_panel.time_ticks(Recent(one_day, with_ma(one_day)))[1] == ["09:15", "11:15", "12:00"]
    late = candles(16, 14, 15, 5) + candles(19, 9, 15, 4)  # the earlier session has no 09:15 / odd hour of its own
    assert candle_panel.time_ticks(Recent(late, with_ma(late)))[1] == ["14:15\n16 Oct", "09:15\n19 Oct", "10:00"]
