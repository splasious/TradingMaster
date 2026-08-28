"""Technical indicator engine (PRD section 12).

Every indicator here is a pure function of past-and-current bars only --
none of them peek forward. That's what makes them safe to reuse unchanged
in the backtesting engine (Phase 5) once it exists: an indicator value
computed at bar N never depends on bar N+1.

The one deliberate exception is `structure.swing_high_low`, which is a
*retrospective* chart annotation (by definition a swing point needs bars on
both sides to confirm) -- it's fine for the Charts/Scanner screens, which
only ever display already-closed history, but a causal/confirmed-N-bars-
later variant would be needed before any strategy could act on it live.
"""

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from app.models.market_data import OhlcvCandle


def candles_to_frame(candles: list[OhlcvCandle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(
        {
            "ts": [c.ts for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume if c.volume is not None else 0.0 for c in candles],
        }
    )
    return df.sort_values("ts").reset_index(drop=True)


def frame_to_series(df: pd.DataFrame, ts: pd.Series, fields: list[str]) -> list[dict]:
    out = []
    for i in range(len(df)):
        point = {"ts": ts.iloc[i]}
        for f in fields:
            value = df[f].iloc[i]
            point[f] = None if pd.isna(value) else round(float(value), 6)
        out.append(point)
    return out


@dataclass
class IndicatorSpec:
    code: str
    name: str
    category: str
    output_fields: list[str]
    default_params: dict[str, float] = field(default_factory=dict)
    compute: Callable[[pd.DataFrame], pd.DataFrame] = None  # type: ignore[assignment]
