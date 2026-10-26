"""The PCR Analysis tab's live row (26 Oct): the Change in OI table's pinned
"LIVE" row -- every field of a 15-minute record as it reads right now,
against the latest record (pcr_snapshots.live_reading), never saved.

Only while NSE trades (09:15-15:30 IST on a trading day) and Zerodha is
logged in. One reading serves every viewer for LIVE_TTL: a reading is one
or two Kite /quote calls (about 1,000 contracts) plus NIFTY's LTP, so
however many pages poll it, Kite is asked at most once per LIVE_TTL.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

from app.db.session import AsyncSessionLocal
from app.services.backfill_platform.coverage import IST, is_trading_day
from app.services.broker.zerodha_broker import KiteAPIError
from app.services.options.pcr_snapshot_scheduler import NOT_LOGGED_IN, kite_broker
from app.services.options.pcr_snapshots import live_reading

logger = logging.getLogger(__name__)

LIVE_TTL = timedelta(seconds=15)
OPEN, CLOSE = time(9, 15), time(15, 30)
CLOSED = "market closed -- the live row shows 09:15-15:30 IST"
KITE_FAILED = "Kite didn't answer the live quote -- trying again shortly"

_cache: dict[str, tuple[datetime, dict | None, str | None]] = {}
_lock = asyncio.Lock()


def market_open(now: datetime) -> bool:
    t = now.astimezone(IST)
    return is_trading_day(t.date()) and OPEN <= t.time() <= CLOSE


async def live_row(underlying: str, now: datetime | None = None) -> tuple[dict | None, str | None]:
    """(the live row, None), or (None, why there isn't one)."""
    now = now or datetime.now(timezone.utc)
    if not market_open(now):
        return None, CLOSED
    async with _lock:  # a second viewer waits for the first one's reading, then gets it from the cache
        hit = _cache.get(underlying)
        if hit is not None and now - hit[0] < LIVE_TTL:
            return hit[1], hit[2]
        row, reason = None, None
        try:
            async with AsyncSessionLocal() as db:
                broker = await kite_broker(db)
                if broker is None:
                    reason = NOT_LOGGED_IN
                else:
                    row = await live_reading(db, broker, underlying, now)
        except KiteAPIError as exc:
            logger.warning("Live PCR reading for %s: Kite failed (%s)", underlying, type(exc).__name__)
            reason = KITE_FAILED
        except Exception:
            logger.exception("Live PCR reading for %s failed", underlying)
            reason = KITE_FAILED
        _cache[underlying] = (now, row, reason)
        return row, reason
