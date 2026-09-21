"""NSE trading-holiday calendar -- the gap hours.py's nse_market_open()
documented from the start: a real trading holiday (Diwali, Republic Day,
...) falls on an ordinary weekday inside the 09:15-15:30 IST window and
wasn't recognized as "closed" until this. Without it, a native or paper
strategy would trade a holiday the exact same way it once traded a
Saturday -- against a frozen tick-engine price, from a market that was
never actually open (see paper_trading/scheduler.py's tick_once()
docstring for that incident).

Two layers, in priority order for a given year:
  1. `refresh_from_nse()` -- a best-effort live fetch of NSE's own public
     holiday-master API, meant to be called periodically by
     nse_holiday_sync_scheduler.py. On success it becomes the
     authoritative source for whatever year(s) it returns.
  2. `STATIC_HOLIDAYS` -- a hand-seeded fallback, used whenever the live
     fetch has never succeeded yet (fresh process start) or is currently
     failing. nseindia.com's own site blocks a bare, cookie-less request
     (confirmed: a direct fetch attempt just hangs/times out) -- the same
     reason this module never assumes the live fetch will work, and never
     lets a failed one make a real holiday invisible.

STATIC_HOLIDAYS was seeded 2026-09-21 from zerodha.com/marketintel/
holiday-calendar and calendarlabs.com/nse-market-holidays-2026 (cross-
checked against each other, since nseindia.com itself couldn't be fetched
directly for this) -- not NSE's own circular directly. Reconcile it
against NSE's official circular (nseindia.com -> Resources -> Exchange
Communication -> Holidays) when convenient, and again every December when
the next year's calendar is published; it has no entries past what's
seeded below. `refresh_from_nse()` succeeding in production supersedes
this for whichever years it covers, so this fallback matters most before
that first successful refresh, or if NSE's site blocks this server
outright (common for datacenter/VPS IPs).
"""

import logging
from datetime import date, datetime, timezone

import httpx

logger = logging.getLogger(__name__)

STATIC_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset({
        date(2026, 1, 26),   # Republic Day
        date(2026, 3, 3),    # Holi
        date(2026, 3, 26),   # Ram Navami
        date(2026, 3, 31),   # Mahavir Jayanti
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
        date(2026, 5, 1),    # Maharashtra Day
        date(2026, 5, 28),   # Bakri Eid / Eid ul-Adha
        date(2026, 6, 26),   # Muharram
        date(2026, 9, 14),   # Ganesh Chaturthi
        date(2026, 10, 2),   # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 10),  # Diwali-Balipratipada
        date(2026, 11, 24),  # Guru Nanak Jayanti
        date(2026, 12, 25),  # Christmas
    }),
}

# NSE's own bot-mitigation rejects a bare API request with no prior
# session -- a plain browser User-Agent plus a warm-up GET of the
# homepage (below, for its cookies) is the standard workaround; still not
# guaranteed to work from every network (NSE is known to block many
# datacenter/VPS IP ranges outright, in which case this always falls
# through to STATIC_HOLIDAYS -- see this module's docstring).
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
}
_HOLIDAY_API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"

_live_holidays: dict[int, frozenset[date]] = {}
last_live_fetch_at: datetime | None = None
last_live_fetch_error: str | None = None


def is_trading_holiday(d: date) -> bool:
    """A live-fetched year always wins over the static fallback for that
    same year -- see module docstring on why the static list is only a
    floor, not a ceiling."""
    live = _live_holidays.get(d.year)
    if live is not None:
        return d in live
    return d in STATIC_HOLIDAYS.get(d.year, frozenset())


async def refresh_from_nse() -> int:
    """Best-effort live refresh from NSE's own public holiday-master API.
    Returns how many holiday dates were parsed and stored (0 on any
    failure -- network, non-200, unexpected shape -- with the reason left
    in last_live_fetch_error for diagnostics, never raised: a failed
    refresh must silently keep whatever this module already had, real or
    static, not take the calendar away)."""
    global last_live_fetch_at, last_live_fetch_error
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10.0, follow_redirects=True) as client:
            await client.get("https://www.nseindia.com")  # cookie warm-up, see _HEADERS' comment
            resp = await client.get(_HOLIDAY_API_URL)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        last_live_fetch_error = f"{type(exc).__name__}: {exc}"
        logger.warning("NSE holiday-calendar refresh failed: %s", last_live_fetch_error)
        return 0

    # Documented shape: {"CM": [...], "FO": [...], ...} -- one row list per
    # market segment, each row like {"tradingDate": "26-Jan-2026",
    # "description": "Republic Day", ...}. FO (derivatives) preferred over
    # CM (equity, cash market) since this app trades NIFTY options; the two
    # segments' trading-holiday calendars are identical in practice, this
    # just picks one deterministically rather than merging duplicates.
    rows = payload.get("FO") or payload.get("CM") or []
    parsed: dict[int, set[date]] = {}
    for row in rows:
        raw = row.get("tradingDate")
        if not raw:
            continue
        try:
            parsed_date = datetime.strptime(raw, "%d-%b-%Y").date()
        except ValueError:
            continue
        parsed.setdefault(parsed_date.year, set()).add(parsed_date)

    if not parsed:
        last_live_fetch_error = "NSE response had no recognizable holiday rows"
        logger.warning("NSE holiday-calendar refresh failed: %s", last_live_fetch_error)
        return 0

    for year, days in parsed.items():
        _live_holidays[year] = frozenset(days)
    last_live_fetch_at = datetime.now(timezone.utc)
    last_live_fetch_error = None
    return sum(len(days) for days in parsed.values())
