"""What open positions are shown at while NSE is shut (15:30 to 09:15,
weekends, holidays): the last session's closing price, read from the saved
candles. The live feed stops at 15:30 and its last tick lives only in
memory, so a restart after the close (an evening deploy) used to blank
LTP, Live Value and P&L on the Trading page until the next open.

The day candle's close (in with the evening download, ~18:00) is the
day's closing price. Until it's in, the newest intraday candle's close --
the last traded price at 15:30 -- stands in for it, marked provisional.

Display only: the strategies and the live engine's loss limit keep
reading live prices exactly as before."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform.coverage import ist_date, last_completed_session, nse_bar_end, session_close
from app.services.market_data.hours import nse_market_open
from app.services.market_data.tick_engine import tick_engine

INTRADAY = ("5m", "15m", "30m", "60m")
# How far back a close is looked for -- past any NSE holiday run, and it
# keeps the lookup to a fortnight of candles on a page that refreshes often.
LOOKBACK = timedelta(days=14)


@dataclass(frozen=True)
class SessionClose:
    price: float
    session: date  # the trading day it is the close of
    provisional: bool  # the 15:30 last price, until that day's candle is saved
    as_of: datetime  # when the price stood: the session close, or the intraday bar's end


async def _latest(db: AsyncSession, ids: list[uuid.UUID], timeframes: tuple[str, ...], before: datetime) -> dict:
    newest = (
        select(OhlcvCandle.instrument_id, func.max(OhlcvCandle.ts).label("ts"))
        .where(
            OhlcvCandle.instrument_id.in_(ids), OhlcvCandle.timeframe.in_(timeframes),
            OhlcvCandle.ts >= before - LOOKBACK, OhlcvCandle.ts < before,
        )
        .group_by(OhlcvCandle.instrument_id)
        .subquery()
    )
    rows = await db.execute(
        select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.timeframe, OhlcvCandle.close)
        .join(newest, (OhlcvCandle.instrument_id == newest.c.instrument_id) & (OhlcvCandle.ts == newest.c.ts))
        .where(OhlcvCandle.timeframe.in_(timeframes))
        .order_by(OhlcvCandle.timeframe)
    )
    latest: dict = {}
    for instrument_id, ts, timeframe, close in rows:
        latest.setdefault(instrument_id, (as_aware_utc(ts), timeframe, close))  # bars sharing a start end together
    return latest


async def session_closes(db: AsyncSession, instrument_ids, now: datetime) -> dict[uuid.UUID, SessionClose]:
    """Each instrument's close for the last completed session at `now`
    (an older one if that's all that's saved; none without a candle in
    the last LOOKBACK)."""
    ids = list(dict.fromkeys(instrument_ids))
    if not ids:
        return {}
    before = session_close(last_completed_session(now))
    daily = await _latest(db, ids, ("1d",), before)
    intraday = await _latest(db, ids, INTRADAY, before)
    closes: dict[uuid.UUID, SessionClose] = {}
    for instrument_id in ids:
        day, bar = daily.get(instrument_id), intraday.get(instrument_id)
        if day is not None and (bar is None or ist_date(day[0]) >= ist_date(bar[0])):
            session = ist_date(day[0])
            closes[instrument_id] = SessionClose(float(day[2]), session, False, session_close(session))
        elif bar is not None:
            closes[instrument_id] = SessionClose(float(bar[2]), ist_date(bar[0]), True, nse_bar_end(bar[0], bar[1]))
    return closes


async def display_prices(db: AsyncSession, instrument_ids, now: datetime | None = None) -> dict[uuid.UUID, tuple[float | None, SessionClose | None]]:
    """Price to show for each instrument, and the close it is when NSE is
    shut: the live price while the market is open; the session close while
    it's shut -- or, with no candle saved at all, the last live price."""
    now = now or datetime.now(timezone.utc)
    ids = list(dict.fromkeys(instrument_ids))
    closes = {} if nse_market_open(now) else await session_closes(db, ids, now)
    shown = {}
    for instrument_id in ids:
        close = closes.get(instrument_id)
        shown[instrument_id] = (close.price, close) if close else (tick_engine.get_current_price(instrument_id), None)
    return shown
