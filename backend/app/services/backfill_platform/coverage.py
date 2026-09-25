"""What is saved, and how fresh it is.

bf_coverage holds one row per symbol and timeframe -- first/last bar, bar
count, bars on the last saved day -- kept current by every save. It is the
watermark the daily top-up fetches after, and what every "saved up to"
status reads: aggregating bf_ohlcv_bars itself (20M+ rows) took 35-120s.

Freshness is measured in NSE sessions (09:15-15:30 IST, trading days only):
a session counts as saved once its last bar is in, and "sessions behind"
is how many completed sessions after that are missing.
"""

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfCoverage, BfOhlcvBar, BfSettings
from app.services.market_data.bar_periods import BAR_DURATIONS
from app.services.market_data.nse_holidays import is_trading_holiday

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
# Bars in a full 09:15-15:30 session -- fewer on the last saved day marks a
# partial day. (Thinly traded contracts legitimately skip bars, so this is
# only judged for NSE equity.)
NSE_FULL_DAY_BARS = {"1m": 375, "5m": 75, "15m": 25, "30m": 13, "60m": 7, "1d": 1}
KITE_SOURCES = ("zerodha", "zerodha_nfo")


def ist_date(ts: datetime) -> date:
    return as_aware_utc(ts).astimezone(IST).date()


def session_close(d: date) -> datetime:
    return datetime.combine(d, SESSION_CLOSE, tzinfo=IST)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_trading_holiday(d)


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def last_completed_session(now: datetime) -> date:
    """The most recent trading day whose session has closed at `now`."""
    today = as_aware_utc(now).astimezone(IST).date()
    if is_trading_day(today) and as_aware_utc(now) >= session_close(today):
        return today
    return previous_trading_day(today)


# The live sync keeps going this long past the window's end, so the candles
# ending at the close (e.g. 15:15-15:30) are saved once final.
LIVE_GRACE = timedelta(minutes=5)


def parse_hhmm(value: str, fallback: time) -> time:
    try:
        hours, minutes = (int(part) for part in value.split(":"))
        return time(hours, minutes)
    except (ValueError, AttributeError):
        return fallback


def live_window_open(now: datetime, start: str = "09:00", end: str = "15:30") -> bool:
    """Whether the live candle sync runs: an NSE trading day, between the
    configured start and end (IST), plus LIVE_GRACE."""
    ist = as_aware_utc(now).astimezone(IST)
    if not is_trading_day(ist.date()):
        return False
    opens = datetime.combine(ist.date(), parse_hhmm(start, time(9, 0)), tzinfo=IST)
    closes = datetime.combine(ist.date(), parse_hhmm(end, SESSION_CLOSE), tzinfo=IST) + LIVE_GRACE
    return opens <= ist <= closes


async def live_sync_open(db: AsyncSession, now: datetime) -> bool:
    settings = await get_settings(db)
    return live_window_open(now, settings.live_start, settings.live_end)


def nse_bar_end(ts: datetime, timeframe: str) -> datetime:
    """When the candle opening at `ts` is final: its period's end, but no
    later than the session close -- the 15:15 60-minute candle is final at
    15:30, and a daily candle once that day's session has closed."""
    ts = as_aware_utc(ts)
    if timeframe == "1d":
        return session_close(ist_date(ts))
    end = ts + BAR_DURATIONS.get(timeframe, timedelta(0))
    close = session_close(ist_date(ts))
    return min(end, close) if ts < close else end


def nse_bar_complete(ts: datetime, timeframe: str, now: datetime) -> bool:
    return nse_bar_end(ts, timeframe) <= as_aware_utc(now)


def sessions_behind(last_ts: datetime, timeframe: str, now: datetime, checked_through: date | None = None) -> int:
    """Completed sessions not saved: 0 when the last closed session is in --
    or was fetched through (`checked_through`) and simply had no trades."""
    target = last_completed_session(now)
    last_day = ist_date(last_ts)
    # The last saved day only counts once it reaches the close.
    first_missing = last_day if nse_bar_end(last_ts, timeframe) < session_close(last_day) else next_trading_day(last_day)
    if checked_through is not None and checked_through >= first_missing:
        first_missing = next_trading_day(checked_through)
    if not is_trading_day(first_missing):
        first_missing = next_trading_day(first_missing)
    count = 0
    d = first_missing
    while d <= target:
        count += 1
        d = next_trading_day(d)
    return count


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    """The IST day, in UTC -- query bounds, compared against stored UTC."""
    start = datetime.combine(d, time(0, 0), tzinfo=IST).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _upsert(db: AsyncSession):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


async def recompute_coverage(db: AsyncSession, symbol_id: uuid.UUID, timeframe: str) -> None:
    """Refreshes one symbol+timeframe's row from its stored bars -- a range
    read on the (symbol, timeframe, ts) index. Called after each job saves."""
    first_ts, last_ts, count = (
        await db.execute(
            select(func.min(BfOhlcvBar.ts), func.max(BfOhlcvBar.ts), func.count())
            .where(BfOhlcvBar.symbol_id == symbol_id, BfOhlcvBar.timeframe == timeframe)
        )
    ).one()
    if not count:
        await db.execute(delete(BfCoverage).where(BfCoverage.symbol_id == symbol_id, BfCoverage.timeframe == timeframe))
        return
    await _write_coverage(db, symbol_id, timeframe, first_ts, last_ts, count)


async def _write_coverage(
    db: AsyncSession, symbol_id: uuid.UUID, timeframe: str, first_ts: datetime, last_ts: datetime, count: int,
) -> None:
    day_start, day_end = _day_bounds(ist_date(last_ts))
    last_day_bars = (
        await db.execute(
            select(func.count()).where(
                BfOhlcvBar.symbol_id == symbol_id, BfOhlcvBar.timeframe == timeframe,
                BfOhlcvBar.ts >= day_start, BfOhlcvBar.ts < day_end,
            )
        )
    ).scalar_one()
    values = {
        "first_ts": as_aware_utc(first_ts), "last_ts": as_aware_utc(last_ts), "bar_count": count,
        "last_day_bars": last_day_bars, "updated_at": datetime.now(timezone.utc),
    }
    insert = _upsert(db)
    await db.execute(
        insert(BfCoverage).values(symbol_id=symbol_id, timeframe=timeframe, **values)
        .on_conflict_do_update(index_elements=["symbol_id", "timeframe"], set_=values)
    )


async def mark_checked(db: AsyncSession, symbol_id: uuid.UUID, timeframe: str, session: date) -> None:
    """Records that a job fetched this pair through `session` (see
    BfCoverage.checked_through). A pair with no saved bars has no row and
    stays untracked."""
    row = await db.get(BfCoverage, (symbol_id, timeframe))
    if row is not None and (row.checked_through is None or session > row.checked_through):
        row.checked_through = session


async def bump_coverage(db: AsyncSession, symbol_id: uuid.UUID, timeframe: str, new_ts: list[datetime]) -> None:
    """Extends a row by newly inserted bars without re-reading the stored
    ones -- for the minute-by-minute live sync."""
    if not new_ts:
        return
    new_ts = [as_aware_utc(t) for t in new_ts]
    row = await db.get(BfCoverage, (symbol_id, timeframe))
    if row is None:
        await recompute_coverage(db, symbol_id, timeframe)
        return
    newest = max(new_ts)
    row.first_ts = min(as_aware_utc(row.first_ts), min(new_ts))
    if newest > as_aware_utc(row.last_ts):
        same_day = [t for t in new_ts if ist_date(t) == ist_date(newest)]
        row.last_day_bars = (row.last_day_bars or 0) + len(same_day) if ist_date(newest) == ist_date(row.last_ts) else len(same_day)
        row.last_ts = newest
    row.bar_count += len(new_ts)
    row.updated_at = datetime.now(timezone.utc)


async def get_settings(db: AsyncSession) -> BfSettings:
    settings = await db.get(BfSettings, 1)
    if settings is None:  # a fresh database the migration's seed row isn't in (tests)
        settings = BfSettings(id=1)
        db.add(settings)
        await db.flush()
    return settings


async def build_coverage() -> int:
    """Fills bf_coverage from the stored bars once, after the upgrade that
    added it -- in the background, since it reads every bar. Safe to repeat
    (it overwrites each row with the recounted values), so an interrupted
    build simply runs again at the next start."""
    async with AsyncSessionLocal() as db:
        settings = await get_settings(db)
        if settings.coverage_built_at is not None:
            return 0
        pairs = (
            await db.execute(
                select(BfOhlcvBar.symbol_id, BfOhlcvBar.timeframe, func.min(BfOhlcvBar.ts), func.max(BfOhlcvBar.ts), func.count())
                .group_by(BfOhlcvBar.symbol_id, BfOhlcvBar.timeframe)
            )
        ).all()
    built = 0
    for i in range(0, len(pairs), 200):
        async with AsyncSessionLocal() as db:
            for symbol_id, timeframe, first_ts, last_ts, count in pairs[i : i + 200]:
                await _write_coverage(db, symbol_id, timeframe, first_ts, last_ts, count)
                built += 1
            await db.commit()
    async with AsyncSessionLocal() as db:
        (await get_settings(db)).coverage_built_at = datetime.now(timezone.utc)
        await db.commit()
    logger.info("Built backfill coverage for %d symbol/timeframe pairs", built)
    return built
