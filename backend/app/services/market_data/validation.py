"""Data quality checks (PRD section 11). Computed on demand from stored
candles rather than persisted -- it's cheap to recompute and always
reflects the current state of the table, so there's no cache to go stale.
"""

from datetime import timedelta
from typing import TypedDict

from app.models.market_data import OhlcvCandle

DAILY_TIMEFRAMES = ("1d", "1wk", "1mo")


class QualityReport(TypedDict):
    candle_count: int
    invalid_ohlc_count: int
    non_positive_price_count: int
    missing_weekday_gaps: int
    quality_score: float


def compute_quality(candles: list[OhlcvCandle], timeframe: str) -> QualityReport:
    total = len(candles)
    if total == 0:
        return QualityReport(
            candle_count=0, invalid_ohlc_count=0, non_positive_price_count=0, missing_weekday_gaps=0, quality_score=0.0
        )

    invalid_ohlc = 0
    non_positive = 0
    for c in candles:
        if c.open <= 0 or c.high <= 0 or c.low <= 0 or c.close <= 0:
            non_positive += 1
            continue
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
            invalid_ohlc += 1

    missing_gaps = 0
    if timeframe == "1d":
        # Heuristic, not a real exchange calendar: flag any gap of more than
        # one calendar day between consecutive weekday sessions as a
        # possible missing candle. A later phase's market-session engine
        # (PRD section 57) replaces this with actual holiday awareness.
        ordered = sorted(candles, key=lambda c: c.ts)
        for prev, cur in zip(ordered, ordered[1:]):
            gap_days = (cur.ts.date() - prev.ts.date()).days
            if prev.ts.weekday() < 4 and gap_days > 1:  # Mon-Thu followed by a gap
                missing_gaps += gap_days - 1
            elif prev.ts.weekday() == 4 and gap_days > 3:  # Friday -> should resume Monday
                missing_gaps += gap_days - 3

    bad = invalid_ohlc + non_positive + missing_gaps
    quality_score = max(0.0, round((total - bad) / total * 100, 2))

    return QualityReport(
        candle_count=total,
        invalid_ohlc_count=invalid_ohlc,
        non_positive_price_count=non_positive,
        missing_weekday_gaps=missing_gaps,
        quality_score=quality_score,
    )
