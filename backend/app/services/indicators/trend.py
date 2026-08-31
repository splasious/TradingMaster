import numpy as np
import pandas as pd


def sma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    return pd.DataFrame({"sma": df["close"].rolling(window=period, min_periods=period).mean()})


def ema(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    return pd.DataFrame({"ema": df["close"].ewm(span=period, adjust=False, min_periods=period).mean()})


def vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Session VWAP, session = calendar day. For daily-or-coarser candles
    each "session" is a single bar, so VWAP degenerates to that bar's
    typical price -- correct, if not very interesting, for daily charts;
    it becomes meaningful once intraday candles are backfilled."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = pd.to_datetime(df["ts"]).dt.date
    tp_vol = typical * df["volume"]
    cum_tp_vol = tp_vol.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return pd.DataFrame({"vwap": cum_tp_vol / cum_vol.replace(0, np.nan)})


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)


def _wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(window=period, min_periods=period).apply(lambda x: (x * weights).sum() / weights.sum(), raw=True)


def wma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    return pd.DataFrame({"wma": _wma(df["close"], period)})


def dema(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    ema1 = df["close"].ewm(span=period, adjust=False, min_periods=period).mean()
    ema2 = ema1.ewm(span=period, adjust=False, min_periods=period).mean()
    return pd.DataFrame({"dema": 2 * ema1 - ema2})


def tema(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    ema1 = df["close"].ewm(span=period, adjust=False, min_periods=period).mean()
    ema2 = ema1.ewm(span=period, adjust=False, min_periods=period).mean()
    ema3 = ema2.ewm(span=period, adjust=False, min_periods=period).mean()
    return pd.DataFrame({"tema": 3 * ema1 - 3 * ema2 + ema3})


def hma(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Hull Moving Average: WMA(2*WMA(n/2) - WMA(n), sqrt(n)) -- reacts
    faster than a plain WMA/EMA while staying smooth."""
    half = max(1, period // 2)
    sqrt_period = max(1, int(round(period**0.5)))
    raw = 2 * _wma(df["close"], half) - _wma(df["close"], period)
    return pd.DataFrame({"hma": _wma(raw, sqrt_period)})


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index + the +DI/-DI lines it's built from,
    Wilder's original smoothing (equivalent to an EMA with alpha=1/period,
    same convention already used by atr/rsi elsewhere in this package)."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]

    tr = _true_range(df)
    atr_smooth = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smooth
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_smooth

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_line = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return pd.DataFrame({"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di})


def aroon(df: pd.DataFrame, period: int = 25) -> pd.DataFrame:
    window = period + 1

    def _up(x: np.ndarray) -> float:
        return 100 * (period - (len(x) - 1 - int(np.argmax(x)))) / period

    def _down(x: np.ndarray) -> float:
        return 100 * (period - (len(x) - 1 - int(np.argmin(x)))) / period

    aroon_up = df["high"].rolling(window=window, min_periods=window).apply(_up, raw=True)
    aroon_down = df["low"].rolling(window=window, min_periods=window).apply(_down, raw=True)
    return pd.DataFrame({"aroon_up": aroon_up, "aroon_down": aroon_down})


def parabolic_sar(df: pd.DataFrame, af_start: float = 0.02, af_increment: float = 0.02, af_max: float = 0.2) -> pd.DataFrame:
    """Wilder's Parabolic SAR -- inherently iterative/stateful (each bar's
    SAR and acceleration factor depend on the running trend), same as
    supertrend() above; there's no vectorized rolling-window equivalent."""
    n = len(df)
    high, low, close = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    sar = np.full(n, np.nan)
    if n == 0:
        return pd.DataFrame({"sar": sar})

    uptrend = True
    af = af_start
    ep = high[0]
    sar[0] = low[0]

    for i in range(1, n):
        prev_sar = sar[i - 1]
        candidate = prev_sar + af * (ep - prev_sar)
        if uptrend:
            candidate = min(candidate, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < candidate:
                uptrend = False
                sar[i] = ep
                ep = low[i]
                af = af_start
            else:
                sar[i] = candidate
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_increment, af_max)
        else:
            candidate = max(candidate, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > candidate:
                uptrend = True
                sar[i] = ep
                ep = high[i]
                af = af_start
            else:
                sar[i] = candidate
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_increment, af_max)

    return pd.DataFrame({"sar": sar})


def ichimoku(df: pd.DataFrame, tenkan_period: int = 9, kijun_period: int = 26, senkou_b_period: int = 52) -> pd.DataFrame:
    """Tenkan/kijun/senkou spans only -- the chikou (lagging) span is
    deliberately omitted: its standard definition plots today's close 26
    bars in the past, which means reading it "as of" that earlier bar
    would be a genuine look-ahead violation (see base.py's module
    docstring on why swing_high_low is the one documented exception).
    Senkou spans ARE safe: shifting a value forward by the displacement
    uses only data already known at the earlier bar it's computed from."""
    displacement = kijun_period
    tenkan = (df["high"].rolling(tenkan_period, min_periods=tenkan_period).max() + df["low"].rolling(tenkan_period, min_periods=tenkan_period).min()) / 2
    kijun = (df["high"].rolling(kijun_period, min_periods=kijun_period).max() + df["low"].rolling(kijun_period, min_periods=kijun_period).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(displacement)
    senkou_b = (
        (df["high"].rolling(senkou_b_period, min_periods=senkou_b_period).max() + df["low"].rolling(senkou_b_period, min_periods=senkou_b_period).min()) / 2
    ).shift(displacement)
    return pd.DataFrame({"tenkan_sen": tenkan, "kijun_sen": kijun, "senkou_span_a": senkou_a, "senkou_span_b": senkou_b})


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    tr = _true_range(df)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    hl2 = (df["high"] + df["low"]) / 2
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    n = len(df)
    upper: list[float] = [np.nan] * n
    lower: list[float] = [np.nan] * n
    trend: list[float] = [np.nan] * n  # 1 = uptrend, -1 = downtrend

    for i in range(n):
        if np.isnan(atr.iloc[i]):
            continue

        have_prev = i > 0 and not np.isnan(upper[i - 1])
        prev_upper = upper[i - 1] if have_prev else upper_basic.iloc[i]
        prev_lower = lower[i - 1] if have_prev else lower_basic.iloc[i]
        prev_trend = trend[i - 1] if have_prev else 1
        close_prev = df["close"].iloc[i - 1] if have_prev else df["close"].iloc[i]

        cur_upper = upper_basic.iloc[i] if upper_basic.iloc[i] < prev_upper or close_prev > prev_upper else prev_upper
        cur_lower = lower_basic.iloc[i] if lower_basic.iloc[i] > prev_lower or close_prev < prev_lower else prev_lower
        upper[i] = cur_upper
        lower[i] = cur_lower

        close = df["close"].iloc[i]
        if prev_trend == 1:
            trend[i] = -1 if close < cur_lower else 1
        else:
            trend[i] = 1 if close > cur_upper else -1

    line = [lower[i] if trend[i] == 1 else upper[i] for i in range(n)]
    return pd.DataFrame({"supertrend": line, "trend": trend})
