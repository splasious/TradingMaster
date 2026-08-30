"""NSE market-hours heuristic shared by the Data Backfill Platform's live
sync scheduler and the Markets page real price feed -- real weekday +
09:15-15:30 IST window check. Not holiday-aware (same documented limitation
used elsewhere in this codebase, e.g. the quality-panel gap detection)."""

from datetime import datetime, timedelta, timezone

_IST_OFFSET = timedelta(hours=5, minutes=30)


def nse_market_open(now: datetime) -> bool:
    ist = now.astimezone(timezone.utc) + _IST_OFFSET
    if ist.weekday() >= 5:
        return False
    open_t = ist.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= ist <= close_t
