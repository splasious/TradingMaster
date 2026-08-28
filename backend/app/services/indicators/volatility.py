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
