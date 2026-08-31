import numpy as np
import pandas as pd


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)


def rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing (the standard RSI definition), equivalent to an
    # EMA with alpha = 1/period.
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    result = 100 - (100 / (1 + rs))
    result = result.where(avg_loss != 0, 100.0)  # no losses in window -> RSI 100, not NaN from div-by-zero
    return pd.DataFrame({"rsi": result})


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = df["close"].ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "histogram": macd_line - signal_line})


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    lowest_low = df["low"].rolling(window=k_period, min_periods=k_period).min()
    highest_high = df["high"].rolling(window=k_period, min_periods=k_period).max()
    percent_k = 100 * (df["close"] - lowest_low) / (highest_high - lowest_low)
    percent_d = percent_k.rolling(window=d_period, min_periods=d_period).mean()
    return pd.DataFrame({"percent_k": percent_k, "percent_d": percent_d})


def cci(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = typical.rolling(window=period, min_periods=period).mean()
    mean_deviation = (typical - sma_tp).abs().rolling(window=period, min_periods=period).mean()
    return pd.DataFrame({"cci": (typical - sma_tp) / (0.015 * mean_deviation)})


def williams_r(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    highest_high = df["high"].rolling(window=period, min_periods=period).max()
    lowest_low = df["low"].rolling(window=period, min_periods=period).min()
    return pd.DataFrame({"williams_r": -100 * (highest_high - df["close"]) / (highest_high - lowest_low)})


def roc(df: pd.DataFrame, period: int = 12) -> pd.DataFrame:
    prior = df["close"].shift(period)
    return pd.DataFrame({"roc": 100 * (df["close"] - prior) / prior})


def momentum(df: pd.DataFrame, period: int = 10) -> pd.DataFrame:
    return pd.DataFrame({"momentum": df["close"] - df["close"].shift(period)})


def trix(df: pd.DataFrame, period: int = 15) -> pd.DataFrame:
    ema1 = df["close"].ewm(span=period, adjust=False, min_periods=period).mean()
    ema2 = ema1.ewm(span=period, adjust=False, min_periods=period).mean()
    ema3 = ema2.ewm(span=period, adjust=False, min_periods=period).mean()
    return pd.DataFrame({"trix": 100 * ema3.pct_change()})


def ultimate_oscillator(df: pd.DataFrame, period1: int = 7, period2: int = 14, period3: int = 28) -> pd.DataFrame:
    prior_close = df["close"].shift(1)
    bp = df["close"] - pd.concat([df["low"], prior_close], axis=1).min(axis=1)
    tr = pd.concat([df["high"], prior_close], axis=1).max(axis=1) - pd.concat([df["low"], prior_close], axis=1).min(axis=1)

    def avg(period: int) -> pd.Series:
        return bp.rolling(window=period, min_periods=period).sum() / tr.rolling(window=period, min_periods=period).sum()

    uo = 100 * (4 * avg(period1) + 2 * avg(period2) + avg(period3)) / 7
    return pd.DataFrame({"ultimate_oscillator": uo})


def awesome_oscillator(df: pd.DataFrame, fast: int = 5, slow: int = 34) -> pd.DataFrame:
    median_price = (df["high"] + df["low"]) / 2
    ao = median_price.rolling(window=fast, min_periods=fast).mean() - median_price.rolling(window=slow, min_periods=slow).mean()
    return pd.DataFrame({"awesome_oscillator": ao})


def cmo(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Chande Momentum Oscillator."""
    delta = df["close"].diff()
    gain_sum = delta.clip(lower=0).rolling(window=period, min_periods=period).sum()
    loss_sum = (-delta.clip(upper=0)).rolling(window=period, min_periods=period).sum()
    return pd.DataFrame({"cmo": 100 * (gain_sum - loss_sum) / (gain_sum + loss_sum)})


def ppo(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Percentage Price Oscillator -- MACD normalized by price, so it's
    comparable across instruments at different price levels."""
    ema_fast = df["close"].ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False, min_periods=slow).mean()
    ppo_line = 100 * (ema_fast - ema_slow) / ema_slow
    signal_line = ppo_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"ppo": ppo_line, "signal": signal_line, "histogram": ppo_line - signal_line})


def dpo(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Detrended Price Oscillator: a past close minus the SMA ending at the
    current bar -- the classic offset (period/2 + 1 bars back) references
    only already-known data, so it stays causal despite "detrending"
    against an older price point."""
    shift_periods = period // 2 + 1
    sma = df["close"].rolling(window=period, min_periods=period).mean()
    return pd.DataFrame({"dpo": df["close"].shift(shift_periods) - sma})
