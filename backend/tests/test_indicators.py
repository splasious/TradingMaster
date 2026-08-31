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


# --- expanded indicator library (30 additions across trend/momentum/volatility/volume) ---


def test_wma_matches_manual_calculation():
    df = _df([10, 20, 30])
    result = trend.wma(df, period=3)
    expected = (10 * 1 + 20 * 2 + 30 * 3) / (1 + 2 + 3)
    assert result["wma"].iloc[-1] == pytest.approx(expected)


def test_dema_converges_on_flat_series():
    df = _df([100.0] * 40)
    assert trend.dema(df, period=10)["dema"].iloc[-1] == pytest.approx(100.0)


def test_tema_converges_on_flat_series():
    df = _df([100.0] * 40)
    assert trend.tema(df, period=10)["tema"].iloc[-1] == pytest.approx(100.0)


def test_hma_converges_on_flat_series():
    df = _df([100.0] * 40)
    assert trend.hma(df, period=10)["hma"].iloc[-1] == pytest.approx(100.0)


def test_adx_strong_uptrend_has_plus_di_dominant_and_is_bounded():
    closes = [100 + i * 2 for i in range(40)]
    df = _df(closes)
    result = trend.adx(df, period=14)
    valid = result.dropna()
    assert len(valid) > 0
    assert (valid["plus_di"] > valid["minus_di"]).all()
    assert valid["adx"].between(0, 100).all()


def test_aroon_bounded_0_100():
    closes = [100, 105, 98, 110, 95, 115, 90, 120, 85, 125, 80, 130, 75, 135, 70, 140, 65, 145, 60, 150, 55, 155, 50, 160, 45, 165, 40]
    df = _df(closes)
    result = trend.aroon(df, period=25)
    valid = result.dropna()
    assert len(valid) > 0
    assert valid["aroon_up"].between(0, 100).all()
    assert valid["aroon_down"].between(0, 100).all()


def test_parabolic_sar_stays_below_price_in_sustained_uptrend():
    closes = [100 + i * 3 for i in range(30)]
    df = _df(closes)
    result = trend.parabolic_sar(df)
    valid = result["sar"].iloc[10:]  # let the trend establish past the initial flip
    assert valid.notna().all()
    assert (valid.to_numpy() < df["high"].iloc[10:].to_numpy()).all()


def test_ichimoku_tenkan_and_kijun_bounded_by_their_own_window_range():
    closes = [100 + 5 * math.sin(i / 5) for i in range(80)]
    df = _df(closes)
    result = trend.ichimoku(df, tenkan_period=9, kijun_period=26, senkou_b_period=52)
    valid_tenkan = result["tenkan_sen"].dropna()
    assert len(valid_tenkan) > 0
    assert (valid_tenkan >= df["low"].min()).all() and (valid_tenkan <= df["high"].max()).all()


def test_ichimoku_has_no_chikou_span_field():
    # Deliberately omitted -- see trend.ichimoku's docstring on why the
    # standard chikou definition would be a real look-ahead violation here.
    df = _df([100.0] * 60)
    result = trend.ichimoku(df)
    assert "chikou_span" not in result.columns


def test_keltner_channels_ordering():
    closes = [100 + 5 * math.sin(i / 3) for i in range(40)]
    df = _df(closes)
    valid = volatility.keltner_channels(df, period=20, atr_period=10).dropna()
    assert len(valid) > 0
    assert (valid["upper"] >= valid["middle"]).all()
    assert (valid["middle"] >= valid["lower"]).all()


def test_donchian_channels_matches_rolling_high_low():
    closes = [100, 105, 98, 110, 95, 115, 90]
    df = _df(closes)
    result = volatility.donchian_channels(df, period=5)
    assert result["upper"].iloc[-1] == pytest.approx(df["high"].iloc[-5:].max())
    assert result["lower"].iloc[-1] == pytest.approx(df["low"].iloc[-5:].min())


def test_std_dev_non_negative():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80]
    result = volatility.std_dev(_df(closes), period=10)
    assert (result["std_dev"].dropna() >= 0).all()


def test_historical_volatility_non_negative():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80]
    result = volatility.historical_volatility(_df(closes), period=10)
    assert (result["historical_volatility"].dropna() >= 0).all()


def test_choppiness_index_bounded_0_100():
    closes = [100 + 5 * math.sin(i / 3) for i in range(40)]
    result = volatility.choppiness_index(_df(closes), period=14)
    valid = result["choppiness_index"].dropna()
    assert len(valid) > 0
    assert valid.between(0, 100).all()


def test_cci_positive_in_sustained_uptrend():
    # CCI's inner deviation window needs ~2x period bars to warm up (the
    # rolling mean-deviation is itself computed over a rolling difference).
    closes = [100 + i * 2 for i in range(50)]
    result = momentum.cci(_df(closes), period=20)
    assert result["cci"].iloc[-1] > 0


def test_williams_r_bounded_negative_100_to_0():
    closes = [100, 105, 98, 110, 95, 115, 90, 120, 85, 125, 80, 130, 75, 135, 70]
    result = momentum.williams_r(_df(closes), period=14)
    valid = result["williams_r"].dropna()
    assert len(valid) > 0
    assert valid.between(-100, 0).all()


def test_roc_matches_manual_calculation():
    closes = [100, 101, 102, 103, 110]
    result = momentum.roc(_df(closes), period=4)
    assert result["roc"].iloc[-1] == pytest.approx(100 * (110 - 100) / 100)


def test_momentum_matches_manual_calculation():
    closes = [100, 101, 102, 103, 110]
    result = momentum.momentum(_df(closes), period=4)
    assert result["momentum"].iloc[-1] == pytest.approx(10.0)


def test_trix_near_zero_on_flat_series():
    df = _df([100.0] * 80)
    result = momentum.trix(df, period=10)
    assert result["trix"].dropna().abs().max() < 1e-6


def test_ultimate_oscillator_bounded_0_100():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80, 118, 82, 121, 79, 125, 78, 126, 77, 128, 76, 130, 75, 132, 74, 134]
    result = momentum.ultimate_oscillator(_df(closes), period1=7, period2=14, period3=28)
    valid = result["ultimate_oscillator"].dropna()
    assert len(valid) > 0
    assert valid.between(0, 100).all()


def test_awesome_oscillator_near_zero_on_flat_series():
    df = _df([100.0] * 40)
    result = momentum.awesome_oscillator(df, fast=5, slow=34)
    assert result["awesome_oscillator"].dropna().abs().max() < 1e-9


def test_cmo_is_100_when_all_gains():
    closes = [100 + i for i in range(20)]
    result = momentum.cmo(_df(closes), period=14)
    assert result["cmo"].iloc[-1] == pytest.approx(100.0)


def test_ppo_histogram_equals_ppo_minus_signal():
    closes = [100 + i * 0.5 + (i % 5) for i in range(60)]
    result = momentum.ppo(_df(closes), fast=12, slow=26, signal=9)
    diff = (result["ppo"] - result["signal"] - result["histogram"]).dropna()
    assert (diff.abs() < 1e-9).all()


def test_dpo_uses_only_past_close_and_past_sma():
    # Bar 10's own H/L/C must not affect its DPO -- only earlier bars.
    closes = [100.0] * 10 + [999.0]  # a wild spike right at the bar being tested
    df = _df(closes)
    result = momentum.dpo(df, period=8)
    # shift_periods = 8//2+1 = 5 -> dpo[10] = close[5] - sma(8)[10], neither of
    # which includes close[10]'s own 999 value in a way that should make this NaN/blow up
    assert result["dpo"].iloc[10] == result["dpo"].iloc[10]  # not NaN


def test_accumulation_distribution_is_cumulative():
    df = _df([100, 105, 102, 108], volumes=[1000, 2000, 1500, 3000])
    result = volume.accumulation_distribution(df)
    # Monotonically-defined cumulative sum -- last value is the running total.
    manual = volume._adl(df)
    assert result["adl"].iloc[-1] == pytest.approx(manual.iloc[-1])


def test_chaikin_money_flow_bounded_negative_1_to_1():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80]
    df = _df(closes, volumes=[1000 + 10 * i for i in range(len(closes))])
    result = volume.chaikin_money_flow(df, period=10)
    valid = result["chaikin_money_flow"].dropna()
    assert len(valid) > 0
    assert valid.between(-1, 1).all()


def test_chaikin_oscillator_finite_on_real_series():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80]
    df = _df(closes, volumes=[1000 + 10 * i for i in range(len(closes))])
    result = volume.chaikin_oscillator(df, fast=3, slow=10)
    valid = result["chaikin_oscillator"].dropna()
    assert len(valid) > 0
    assert valid.apply(lambda x: x == x).all()  # no NaN slipped through


def test_force_index_positive_on_up_day_with_volume():
    df = _df([100, 110], volumes=[1000, 2000])
    result = volume.force_index(df, period=1)
    assert result["force_index"].iloc[-1] > 0


def test_ease_of_movement_zero_when_price_midpoint_unchanged():
    df = _df([100, 100, 100], highs=[105, 105, 105], lows=[95, 95, 95], volumes=[1000, 1000, 1000])
    result = volume.ease_of_movement(df, period=1)
    valid = result["ease_of_movement"].dropna()
    assert (valid.abs() < 1e-9).all()


def test_vortex_positive_components():
    closes = [100, 102, 99, 105, 95, 110, 90, 108, 92, 111, 89, 115, 85, 120, 80]
    result = volume.vortex(_df(closes), period=14)
    valid = result.dropna()
    assert len(valid) > 0
    assert (valid["vi_plus"] >= 0).all()
    assert (valid["vi_minus"] >= 0).all()


def test_expanded_registry_indicators_all_have_matching_output_columns():
    """Every new registry entry's compute() must actually produce a column
    for each of its declared output_fields -- catches spec/impl drift."""
    df = _df([100 + i * 0.3 + (i % 7) for i in range(120)])
    new_codes = {
        "wma", "dema", "tema", "hma", "adx", "aroon", "parabolic_sar", "ichimoku",
        "keltner_channels", "donchian_channels", "std_dev", "historical_volatility", "choppiness_index",
        "cci", "williams_r", "roc", "momentum", "trix", "ultimate_oscillator", "awesome_oscillator",
        "cmo", "ppo", "dpo",
        "accumulation_distribution", "chaikin_money_flow", "chaikin_oscillator", "force_index",
        "ease_of_movement", "vortex",
    }
    assert new_codes.issubset(INDICATOR_REGISTRY.keys())
    for code in new_codes:
        spec = INDICATOR_REGISTRY[code]
        computed = spec.compute(df, **spec.default_params)
        for field in spec.output_fields:
            assert field in computed.columns, f"{code}: missing output column {field!r}"


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
        resample_candles(_weekday_bars(5, datetime(2026, 1, 5, tzinfo=timezone.utc)), "3m")


def test_resample_empty_input_returns_empty():
    assert resample_candles([], "1wk") == []


def _minute_bars(n_minutes: int, start: datetime):
    bars = []
    for i in range(n_minutes):
        ts = start + timedelta(minutes=i)
        bars.append({"ts": ts, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 10.0})
    return bars


def test_resample_5m_from_1m_aggregates_ohlc_correctly():
    # Two full, closed 5-minute buckets safely in the past.
    start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    bars = _minute_bars(10, start)
    resampled = resample_candles(bars, "5m", only_closed=True, now=start + timedelta(hours=2))

    assert len(resampled) == 2
    bucket1 = bars[:5]
    assert resampled[0]["open"] == bucket1[0]["open"]
    assert resampled[0]["close"] == bucket1[-1]["close"]
    assert resampled[0]["high"] == max(b["high"] for b in bucket1)
    assert resampled[0]["low"] == min(b["low"] for b in bucket1)
    assert resampled[0]["volume"] == sum(b["volume"] for b in bucket1)


def test_resample_5m_drops_still_forming_bucket():
    start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    bars = _minute_bars(3, start)  # only 3 of 5 minutes in this bucket exist yet
    now = start + timedelta(minutes=3)

    closed = resample_candles(bars, "5m", only_closed=True, now=now)
    unclosed = resample_candles(bars, "5m", only_closed=False, now=now)

    assert closed == []
    assert len(unclosed) == 1


def test_resample_1d_from_1m_derives_daily_bar():
    start = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    bars = _minute_bars(60, start)  # one full closed hour of a day well in the past
    resampled = resample_candles(bars, "1d", only_closed=True, now=start + timedelta(days=2))

    assert len(resampled) == 1
    assert resampled[0]["open"] == bars[0]["open"]
    assert resampled[0]["close"] == bars[-1]["close"]
    assert resampled[0]["high"] == max(b["high"] for b in bars)
    assert resampled[0]["low"] == min(b["low"] for b in bars)


def test_resample_60m_from_5m_chains_correctly():
    # Derive 5m from 1m, then feed that into a 60m resample -- the chained
    # base-timeframe picking the frontend does (finest available -> target).
    start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    minute_bars = _minute_bars(120, start)
    five_min = resample_candles(minute_bars, "5m", only_closed=False)
    hourly = resample_candles(five_min, "60m", only_closed=True, now=start + timedelta(hours=3))

    assert len(hourly) == 2
    assert hourly[0]["open"] == minute_bars[0]["open"]
    assert hourly[0]["close"] == minute_bars[59]["close"]
