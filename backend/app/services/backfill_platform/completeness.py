"""Real per-day completeness segments for the calendar-heatmap view (PRD
section 6). Computed from actually-stored bars, day-granularity even for
intraday timeframes (does this day have at least one bar?) -- a calendar
heatmap is inherently a per-day view, so bar-by-bar gaps within a day
aren't distinguished here.

Weekend-aware for equity sources (Yahoo/Zerodha, NSE doesn't trade
Sat/Sun) but not for Delta (crypto trades every day). Not exchange-holiday
aware -- same documented heuristic gap as the main platform's quality
panel (services/market_data/validation.py); a real holiday calendar is a
separate, ongoing-maintenance data source this doesn't invent.
"""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class CompletenessSegment:
    start: date
    end: date
    status: str  # "filled" | "gap"


def compute_completeness(bar_dates: set[date], start: date, end: date, source: str) -> list[CompletenessSegment]:
    skip_weekends = source in ("yahoo", "zerodha")
    segments: list[CompletenessSegment] = []
    current_status: str | None = None
    seg_start: date | None = None
    prev_d: date | None = None

    d = start
    while d <= end:
        if skip_weekends and d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        status = "filled" if d in bar_dates else "gap"
        if status != current_status:
            if current_status is not None and seg_start is not None and prev_d is not None:
                segments.append(CompletenessSegment(start=seg_start, end=prev_d, status=current_status))
            seg_start = d
            current_status = status
        prev_d = d
        d += timedelta(days=1)

    if current_status is not None and seg_start is not None and prev_d is not None:
        segments.append(CompletenessSegment(start=seg_start, end=prev_d, status=current_status))

    return segments
