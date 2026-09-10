"""Detects when a strategy's signal-driving candle data has gone stale --
the gap between "the trading engine ran and got SOME candles" and "those
candles are recent enough to trust for a real decision" that neither
paper_trading/engine.py nor live_trading/oms.py checked before today.

Gated by nse_market_open: a normal overnight/weekend gap (candles
genuinely don't advance while the market's closed) is never mistaken for
a broken sync pipeline. Not holiday-aware -- same acknowledged limitation
hours.py already documents for itself; the per-timeframe thresholds below
are deliberately generous for daily-and-up timeframes specifically to
absorb a normal long weekend without a false positive, at the cost of
being slower to catch a genuine multi-day outage on those timeframes.
"""

from datetime import datetime, timedelta

from app.core.time import as_aware_utc
from app.models.market_data import OhlcvCandle
from app.services.market_data.hours import nse_market_open

# How old the latest candle can be, while the market is open, before it's
# treated as a sync failure rather than "waiting for the next bar to
# close". Intraday: bar duration x3, floor 15 min.
_MAX_AGE: dict[str, timedelta] = {
    "1m": timedelta(minutes=15),
    "5m": timedelta(minutes=15),
    "15m": timedelta(minutes=45),
    "30m": timedelta(minutes=90),
    "60m": timedelta(minutes=180),
    "1d": timedelta(days=4),
    "1wk": timedelta(days=12),
    "1mo": timedelta(days=40),
}


def check_freshness(candles: list[OhlcvCandle], timeframe: str, now: datetime) -> str | None:
    """None when the data is fine to trade on; a human-readable reason
    string when it isn't. Only enforced while NSE is actually open --
    outside market hours no new candle is expected, so nothing here counts
    as "stale", it's just quiet."""
    if not nse_market_open(now):
        return None
    if not candles:
        return "no candle data available for this instrument/timeframe"
    max_age = _MAX_AGE.get(timeframe)
    if max_age is None:
        return None  # unknown timeframe -- nothing to compare against, not this function's job to reject it

    age = now - as_aware_utc(candles[-1].ts)
    if age > max_age:
        return f"latest candle is {age} old (max {max_age} while market is open) -- sync may be failing"
    return None
