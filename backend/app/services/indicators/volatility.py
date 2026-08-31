import numpy as np
import pandas as pd


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    tr = _true_range(df)
    return pd.DataFrame({"atr": tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()})


def bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    middle = df["close"].rolling(window=period, min_periods=period).mean()
    std = df["close"].rolling(window=period, min_periods=period).std(ddof=0)
    return pd.DataFrame({"upper": middle + std_dev * std, "middle": middle, "lower": middle - std_dev * std})


def keltner_channels(df: pd.DataFrame, period: int = 20, atr_period: int = 10, multiplier: float = 2.0) -> pd.DataFrame:
    middle = df["close"].ewm(span=period, adjust=False, min_periods=period).mean()
    tr = _true_range(df)
    atr_line = tr.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()
    return pd.DataFrame({"upper": middle + multiplier * atr_line, "middle": middle, "lower": middle - multiplier * atr_line})


def donchian_channels(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    upper = df["high"].rolling(window=period, min_periods=period).max()
    lower = df["low"].rolling(window=period, min_periods=period).min()
    return pd.DataFrame({"upper": upper, "middle": (upper + lower) / 2, "lower": lower})


def std_dev(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    return pd.DataFrame({"std_dev": df["close"].rolling(window=period, min_periods=period).std(ddof=0)})


def historical_volatility(df: pd.DataFrame, period: int = 20, trading_days: int = 252) -> pd.DataFrame:
    """Annualized close-to-close volatility, as a percentage."""
    log_returns = np.log(df["close"] / df["close"].shift(1))
    vol = log_returns.rolling(window=period, min_periods=period).std(ddof=0) * (trading_days**0.5) * 100
    return pd.DataFrame({"historical_volatility": vol})


def choppiness_index(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """0-100: high = choppy/range-bound, low = strongly trending."""
    tr = _true_range(df)
    tr_sum = tr.rolling(window=period, min_periods=period).sum()
    high_max = df["high"].rolling(window=period, min_periods=period).max()
    low_min = df["low"].rolling(window=period, min_periods=period).min()
    chop = 100 * np.log10(tr_sum / (high_max - low_min)) / np.log10(period)
    return pd.DataFrame({"choppiness_index": chop})
