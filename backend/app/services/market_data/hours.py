"""NSE market-hours heuristic -- real weekday + 09:15-15:30 IST window,
plus NSE's own trading-holiday calendar (nse_holidays.py: a live NSE fetch
when available, a hand-seeded fallback otherwise). Every consumer of this
function (paper/native trading schedulers, the Kite ticker, the OI/candle
sync jobs) goes quiet on a real trading holiday the same way it already
does on a weekend."""

from datetime import datetime, timedelta, timezone

from app.services.market_data.nse_holidays import is_trading_holiday

_IST_OFFSET = timedelta(hours=5, minutes=30)


def nse_market_open(now: datetime) -> bool:
    ist = now.astimezone(timezone.utc) + _IST_OFFSET
    if ist.weekday() >= 5:
        return False
    if is_trading_holiday(ist.date()):
        return False
    open_t = ist.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= ist <= close_t
