import numpy as np
import pandas as pd


def pivot_points(df: pd.DataFrame) -> pd.DataFrame:
    """Classic pivots for bar N, computed from bar N-1's H/L/C -- causal by
    construction (today's pivot uses yesterday's range, never today's)."""
    prev_high = df["high"].shift(1)
    prev_low = df["low"].shift(1)
    prev_close = df["close"].shift(1)
    pivot = (prev_high + prev_low + prev_close) / 3
    return pd.DataFrame(
        {
            "pivot": pivot,
            "r1": 2 * pivot - prev_low,
            "s1": 2 * pivot - prev_high,
            "r2": pivot + (prev_high - prev_low),
            "s2": pivot - (prev_high - prev_low),
        }
    )


def swing_high_low(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """Retrospective-only: a bar N bars from either edge is a swing
    high/low if it's the max/min of its `window`-bar neighborhood on BOTH
    sides. Do not feed this into live signals or backtests -- confirming a
    swing point requires bars that don't exist yet at the time it forms.
    See base.py's module docstring."""
    highs = df["high"]
    lows = df["low"]
    n = len(df)
    swing_high = [False] * n
    swing_low = [False] * n
    for i in range(window, n - window):
        neighborhood_high = highs.iloc[i - window : i + window + 1]
        neighborhood_low = lows.iloc[i - window : i + window + 1]
        if highs.iloc[i] == neighborhood_high.max() and (neighborhood_high == neighborhood_high.max()).sum() == 1:
            swing_high[i] = True
        if lows.iloc[i] == neighborhood_low.min() and (neighborhood_low == neighborhood_low.min()).sum() == 1:
            swing_low[i] = True
    return pd.DataFrame(
        {
            "swing_high": [df["high"].iloc[i] if swing_high[i] else np.nan for i in range(n)],
            "swing_low": [df["low"].iloc[i] if swing_low[i] else np.nan for i in range(n)],
        }
    )
