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
