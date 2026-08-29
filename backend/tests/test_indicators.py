import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.services.indicators import momentum, structure, trend, volatility, volume
from app.services.indicators.base import candles_to_frame
from app.services.indicators.registry import INDICATOR_REGISTRY, get_indicator
from app.services.market_data.resample import resample_candles


def _df(closes: list[float], highs=None, lows=None, volumes=None, start=None) -> pd.DataFrame:
    n = len(closes)
    start = start or datetime(2026, 1, 5, tzinfo=timezone.utc)  # Monday
    ts = [start + timedelta(days=i) for i in range(n)]
    highs = highs or [c + 1 for c in closes]
    lows = lows or [c - 1 for c in closes]
    volumes = volumes or [1000.0] * n
    return pd.DataFrame({"ts": ts, "open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes})


def test_registry_covers_all_categories():
    categories = {spec.category for spec in INDICATOR_REGISTRY.values()}
    assert categories == {"trend", "momentum", "volume", "volatility", "structure"}


def test_get_indicator_unknown_raises():
    with pytest.raises(ValueError):
        get_indicator("does_not_exist")


def test_sma_matches_manual_rolling_mean():
    closes = [10, 11, 12, 13, 14, 15, 16]
    df = _df(closes)
    result = trend.sma(df, period=3)
    # First two rows have insufficient history -> NaN
    assert result["sma"].iloc[:2].isna().all()
    assert result["sma"].iloc[2] == pytest.approx((10 + 11 + 12) / 3)
    assert result["sma"].iloc[-1] == pytest.approx((14 + 15 + 16) / 3)


def test_ema_is_close_to_final_price_after_long_convergence():
    closes = [100.0] * 30  # flat series -> EMA converges to 100
    df = _df(closes)
    result = trend.ema(df, period=10)
    assert result["ema"].iloc[-1] == pytest.approx(100.0)


def test_rsi_is_100_when_all_gains():
    closes = [100 + i for i in range(20)]  # strictly increasing
    df = _df(closes)
    result = momentum.rsi(df, period=14)
    assert result["rsi"].iloc[-1] == pytest.approx(100.0)


def test_rsi_bounded_0_100_on_mixed_series():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80, 118, 82, 121, 79, 125]
    df = _df(closes)
    result = momentum.rsi(df, period=14)
    valid = result["rsi"].dropna()
    assert len(valid) > 0
    assert valid.between(0, 100).all()


def test_macd_histogram_equals_macd_minus_signal():
    closes = [100 + i * 0.5 + (i % 5) for i in range(60)]
    df = _df(closes)
    result = momentum.macd(df, fast=12, slow=26, signal=9)
    diff = (result["macd"] - result["signal"] - result["histogram"]).dropna()
    assert (diff.abs() < 1e-9).all()


def test_stochastic_bounded_0_100():
    closes = [100, 105, 98, 110, 95, 115, 90, 120, 85, 125, 80, 130, 75, 135, 70, 140]
    df = _df(closes)
    result = momentum.stochastic(df, k_period=14, d_period=3)
    valid_k = result["percent_k"].dropna()
    assert valid_k.between(0, 100).all()


def test_bollinger_bands_ordering():
    closes = [100 + 5 * math.sin(i / 3) for i in range(40)]
    df = _df(closes)
    result = volatility.bollinger_bands(df, period=20, std_dev=2.0)
    valid = result.dropna()
    assert (valid["upper"] >= valid["middle"]).all()
    assert (valid["middle"] >= valid["lower"]).all()


def test_atr_non_negative():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80]
    df = _df(closes)
    result = volatility.atr(df, period=14)
    valid = result["atr"].dropna()
    assert (valid >= 0).all()


def test_obv_increases_on_up_day_decreases_on_down_day():
    df = _df([100, 105, 102, 108], volumes=[1000, 2000, 1500, 3000])
    result = volume.obv(df)
    # day1: no prior -> 0 contribution; day2: up -> +2000; day3: down -> -1500; day4: up -> +3000
    assert result["obv"].iloc[0] == 0
    assert result["obv"].iloc[1] == 2000
    assert result["obv"].iloc[2] == 2000 - 1500
    assert result["obv"].iloc[3] == 2000 - 1500 + 3000


def test_mfi_bounded_0_100():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80]
    df = _df(closes)
    result = volume.mfi(df, period=14)
    valid = result["mfi"].dropna()
    assert valid.between(0, 100).all()


def test_pivot_points_use_previous_bar_only():
    df = _df([100, 110], highs=[105, 999], lows=[95, 1])  # 2nd bar's own H/L must not affect its pivot
    result = structure.pivot_points(df)
    assert result["pivot"].iloc[0] != result["pivot"].iloc[0]  # NaN: no previous bar
    expected_pivot = (105 + 95 + 100) / 3  # from bar 0's H/L/C
    assert result["pivot"].iloc[1] == pytest.approx(expected_pivot)


def test_swing_high_low_detects_single_peak():
    closes = [100, 101, 102, 110, 103, 102, 101]  # clear peak at index 3
    df = _df(closes)
    result = structure.swing_high_low(df, window=2)
    assert result["swing_high"].iloc[3] == pytest.approx(111.0)  # high = close+1
    assert result["swing_high"].dropna().shape[0] == 1


def test_supertrend_produces_only_valid_trend_values():
    closes = [100 + i * 0.7 + (3 if i % 4 == 0 else 0) for i in range(40)]
    df = _df(closes)
    result = trend.supertrend(df, period=10, multiplier=3.0)
    valid = result["trend"].dropna()
    assert set(valid.unique()).issubset({1.0, -1.0})


def test_vwap_positive_and_present():
    df = _df([100, 101, 99, 102])
    result = trend.vwap(df)
    assert (result["vwap"].dropna() > 0).all()


def test_candles_to_frame_empty_list_returns_empty_frame():
    df = candles_to_frame([])
    assert df.empty
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]


# --- resample.py (multi-timeframe engine, PRD section 13) ---


def _weekday_bars(n_days: int, start: datetime):
    """n_days consecutive Mon-Fri trading sessions starting on `start`
    (which must itself be a Monday) -- real market data has no weekend
    bars, and W-FRI weekly bins are only clean to reason about against
    business-day data."""
    bars = []
    d = start
    for i in range(n_days):
        bars.append({"ts": d, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 1000.0})
        d += timedelta(days=1)
        if d.weekday() >= 5:  # skip Sat/Sun
            d += timedelta(days=7 - d.weekday())
    return bars


def test_resample_weekly_aggregates_ohlc_correctly_and_drops_incomplete_week():
    # Two full past weeks (Mon-Fri), safely before "today" so neither is
    # treated as still forming.
    start = datetime(2020, 1, 6, tzinfo=timezone.utc)  # a Monday
    bars = _weekday_bars(10, start)  # exactly two full Mon-Fri weeks
    resampled = resample_candles(bars, "1wk", only_closed=True)

    assert len(resampled) == 2
    week1 = bars[:5]
    assert resampled[0]["open"] == week1[0]["open"]
    assert resampled[0]["close"] == week1[-1]["close"]
    assert resampled[0]["high"] == max(b["high"] for b in week1)
    assert resampled[0]["low"] == min(b["low"] for b in week1)
    assert resampled[0]["volume"] == sum(b["volume"] for b in week1)

    week2 = bars[5:]
    assert resampled[1]["open"] == week2[0]["open"]
    assert resampled[1]["close"] == week2[-1]["close"]


def test_resample_drops_still_forming_period_by_default():
    # A week containing "as_of" must not appear as a closed candle. "now" is
    # passed explicitly (rather than read from the real clock at call time)
    # so this can't flake if the real clock happens to cross a day boundary
    # between building `bars` and resample_candles checking "today" --
    # every date in this test is relative to the same fixed `as_of`.
    as_of = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)  # a Friday, mid-session
    monday_this_week = (as_of - timedelta(days=as_of.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    days_elapsed_this_week = min(as_of.weekday(), 4) + 1  # Mon=1 .. Fri=5 sessions so far
    bars = _weekday_bars(5 + days_elapsed_this_week, monday_this_week - timedelta(days=7))

    closed = resample_candles(bars, "1wk", only_closed=True, now=as_of)
    unclosed = resample_candles(bars, "1wk", only_closed=False, now=as_of)

    assert len(unclosed) == len(closed) + 1  # the forming week only appears when explicitly allowed


def test_resample_rejects_unsupported_timeframe():
    with pytest.raises(ValueError):
        resample_candles(_weekday_bars(5, datetime(2026, 1, 5, tzinfo=timezone.utc)), "5m")


def test_resample_empty_input_returns_empty():
    assert resample_candles([], "1wk") == []
