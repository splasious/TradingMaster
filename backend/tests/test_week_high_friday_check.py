"""The one-off look at 52 Week High Breakout's open positions at last
Friday's 3 PM (scripts/week_high_friday_check.py, 30 Oct): its own copy
of version 3's week -- the stored days plus the check day's 5-minute
candles up to 15:00 -- matches the strategy's."""

import importlib.util
import pathlib
from datetime import date, datetime, time, timedelta

from app.services.broker.zerodha_broker import IST
from app.services.strategy.native_strategies import week_high_breakout_52 as wk

spec = importlib.util.spec_from_file_location("friday_check", pathlib.Path(__file__).parent.parent / "scripts/week_high_friday_check.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)


def test_the_check_days_bar_stops_at_3pm_and_the_week_matches_the_strategys():
    day = date(2026, 10, 23)
    five = [{"ts": datetime.combine(day, time(9, 15), tzinfo=IST) + timedelta(minutes=5 * k), "open": 100 + k, "high": 101 + k,
             "low": 99 + k, "close": 100.5 + k} for k in range(75)]  # 09:15 .. 15:25
    bar = fc.day_until(five, day, datetime.combine(day, time(15, 0), tzinfo=IST))
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (100, 169, 99, 168.5)  # the 14:55 candle (the 69th) is the last

    days = []
    d = day - timedelta(weeks=60)
    while d < day:
        if wk._is_trading_day(d):
            days.append({"day": d, "open": 99.0, "high": 100.0, "low": 98.0, "close": 99.0})
        d += timedelta(days=1)
    this_week = fc.monday(day)
    assert fc.week_so_far(days, bar, this_week, day) == wk.week_so_far(days, bar, this_week, day)
    v = fc.verdict(fc.week_so_far(days, bar, this_week, day))
    assert v["keep"] and round(v["over_high"], 2) == 68.5
    assert fc.week_so_far(days[:-1], bar, this_week, day) is None  # Thursday missing
