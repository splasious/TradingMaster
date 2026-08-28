"""Multi-timeframe engine (PRD section 13).

Critical requirement from the PRD: "Higher-timeframe information becomes
available only after the corresponding candle has actually closed." A
resampled weekly/monthly bar built from the days seen so far this week/month
would otherwise look like a closed candle while the week is still forming --
classic look-ahead bias. `only_closed=True` (the default) drops that last,
still-forming bar.
"""

from datetime import datetime, timezone

import pandas as pd

from app.services.market_data.base import Bar

# W-FRI: weeks ending Friday (matches NSE/most equity markets' Mon-Fri
# sessions). ME: calendar month end.
_FREQ_MAP = {"1wk": "W-FRI", "1mo": "ME"}


def resample_candles(bars: list[Bar], target_timeframe: str, only_closed: bool = True) -> list[Bar]:
    freq = _FREQ_MAP.get(target_timeframe)
    if freq is None:
        raise ValueError(f"Cannot resample to '{target_timeframe}' (supported: {sorted(_FREQ_MAP)})")
    if not bars:
        return []

    df = pd.DataFrame(bars).sort_values("ts")
    df["volume"] = df["volume"].fillna(0.0)
    df = df.set_index(pd.DatetimeIndex(df["ts"]))

    resampled = (
        df.resample(freq)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open"])
    )

    if only_closed and len(resampled):
        today = datetime.now(timezone.utc).date()
        last_period_end = resampled.index[-1].date()
        if today <= last_period_end:
            resampled = resampled.iloc[:-1]

    return [
        Bar(ts=ts.to_pydatetime(), open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume)
        for ts, row in resampled.iterrows()
    ]
