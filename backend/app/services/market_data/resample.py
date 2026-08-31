"""Multi-timeframe engine (PRD section 13).

Critical requirement from the PRD: "Higher-timeframe information becomes
available only after the corresponding candle has actually closed." A
resampled bar built from data seen so far this period would otherwise look
like a closed candle while the period is still forming -- classic
look-ahead bias. `only_closed=True` (the default) drops that last,
still-forming bar.

Two families of target, because pandas anchors them differently:
  - Fixed-duration intraday/daily buckets (5m..1d): each bucket is exactly
    `pd.Timedelta(freq)` long, so "is the last one still forming" is a
    simple `now < bucket_start + freq`. Daily buckets are calendar-day
    (UTC), not exchange-session-day -- for a market whose session doesn't
    align to UTC midnight (e.g. NSE, IST = UTC+5:30) this is an
    approximation, same honestly-documented simplification as this
    codebase's other non-session-aware heuristics (see
    `market_data/hours.py`).
  - Calendar-anchored weekly/monthly (variable-length periods): keep the
    original date-based "is today past this period's end date" check.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.core.time import as_aware_utc
from app.services.market_data.base import Bar

_FIXED_FREQ_MAP = {"5m": "5min", "15m": "15min", "30m": "30min", "60m": "60min", "4h": "4h", "1d": "1D"}
# W-FRI: weeks ending Friday (matches NSE/most equity markets' Mon-Fri
# sessions). ME: calendar month end.
_CALENDAR_FREQ_MAP = {"1wk": "W-FRI", "1mo": "ME"}


def resample_candles(
    bars: list[Bar], target_timeframe: str, only_closed: bool = True, now: datetime | None = None
) -> list[Bar]:
    """`now` defaults to the real current time; tests pass it explicitly so
    a "which period is still forming" assertion can't flake around a
    real-clock boundary between when the test builds its bars and when
    this function checks "now"."""
    freq = _FIXED_FREQ_MAP.get(target_timeframe) or _CALENDAR_FREQ_MAP.get(target_timeframe)
    if freq is None:
        raise ValueError(
            f"Cannot resample to '{target_timeframe}' (supported: {sorted({*_FIXED_FREQ_MAP, *_CALENDAR_FREQ_MAP})})"
        )
    if not bars:
        return []

    bars = [{**bar, "ts": as_aware_utc(bar["ts"])} for bar in bars]
    df = pd.DataFrame(bars).sort_values("ts")
    df["volume"] = df["volume"].fillna(0.0)
    df = df.set_index(pd.DatetimeIndex(df["ts"]))

    resampled = (
        df.resample(freq)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
    )

    if only_closed and len(resampled):
        effective_now = now or datetime.now(timezone.utc)
        if target_timeframe in _FIXED_FREQ_MAP:
            last_bucket_end = resampled.index[-1].to_pydatetime() + timedelta(seconds=pd.Timedelta(freq).total_seconds())
            if effective_now < last_bucket_end:
                resampled = resampled.iloc[:-1]
        else:
            last_period_end = resampled.index[-1].date()
            if effective_now.date() <= last_period_end:
                resampled = resampled.iloc[:-1]

    return [
        Bar(ts=ts.to_pydatetime(), open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume)
        for ts, row in resampled.iterrows()
    ]
