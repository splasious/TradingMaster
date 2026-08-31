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


def _money_flow_multiplier(df: pd.DataFrame) -> pd.Series:
    rng = df["high"] - df["low"]
    return ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng.replace(0, np.nan)


def _adl(df: pd.DataFrame) -> pd.Series:
    return (_money_flow_multiplier(df) * df["volume"]).cumsum()


def accumulation_distribution(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"adl": _adl(df)})


def chaikin_money_flow(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    mfv = _money_flow_multiplier(df) * df["volume"]
    cmf = mfv.rolling(window=period, min_periods=period).sum() / df["volume"].rolling(window=period, min_periods=period).sum()
    return pd.DataFrame({"chaikin_money_flow": cmf})


def chaikin_oscillator(df: pd.DataFrame, fast: int = 3, slow: int = 10) -> pd.DataFrame:
    adl = _adl(df)
    ema_fast = adl.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = adl.ewm(span=slow, adjust=False, min_periods=slow).mean()
    return pd.DataFrame({"chaikin_oscillator": ema_fast - ema_slow})


def force_index(df: pd.DataFrame, period: int = 13) -> pd.DataFrame:
    raw = df["close"].diff() * df["volume"]
    return pd.DataFrame({"force_index": raw.ewm(span=period, adjust=False, min_periods=period).mean()})


def ease_of_movement(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    mid_move = ((df["high"] + df["low"]) / 2).diff()
    box_ratio = (df["volume"] / 1e8) / (df["high"] - df["low"]).replace(0, np.nan)
    emv_raw = mid_move / box_ratio
    return pd.DataFrame({"ease_of_movement": emv_raw.rolling(window=period, min_periods=period).mean()})


def vortex(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    prev_low = df["low"].shift(1)
    prev_high = df["high"].shift(1)
    vm_plus = (df["high"] - prev_low).abs()
    vm_minus = (df["low"] - prev_high).abs()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - df["close"].shift(1)).abs(), (df["low"] - df["close"].shift(1)).abs()], axis=1
    ).max(axis=1)
    tr_sum = tr.rolling(window=period, min_periods=period).sum()
    vi_plus = vm_plus.rolling(window=period, min_periods=period).sum() / tr_sum
    vi_minus = vm_minus.rolling(window=period, min_periods=period).sum() / tr_sum
    return pd.DataFrame({"vi_plus": vi_plus, "vi_minus": vi_minus})
