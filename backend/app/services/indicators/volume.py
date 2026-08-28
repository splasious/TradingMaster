import numpy as np
import pandas as pd


def obv(df: pd.DataFrame) -> pd.DataFrame:
    direction = np.sign(df["close"].diff().fillna(0))
    return pd.DataFrame({"obv": (direction * df["volume"]).cumsum()})


def mfi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    raw_money_flow = typical * df["volume"]
    direction = typical.diff()

    positive_flow = raw_money_flow.where(direction > 0, 0.0).rolling(window=period, min_periods=period).sum()
    negative_flow = raw_money_flow.where(direction < 0, 0.0).rolling(window=period, min_periods=period).sum()

    money_ratio = positive_flow / negative_flow.replace(0, pd.NA)
    result = 100 - (100 / (1 + money_ratio))
    result = result.where(negative_flow != 0, 100.0)
    return pd.DataFrame({"mfi": result})
