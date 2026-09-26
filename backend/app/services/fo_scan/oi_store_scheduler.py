"""Runs the OI store's captures (oi_store.py) on NSE trading days:

  09:10:00-09:14:30  pre-open backup, only if yesterday's close is missing
  09:20:15-09:24:30  every stock's 09:20 OI (after the strategy's own 09:20:07
                     reads, which it would otherwise queue behind)
  15:31:00-17:00:00  the close -- tomorrow's "yesterday"

Checks every TICK_SECONDS; a failed capture (no Zerodha login, a Kite
error) is retried every RETRY until its window ends. A capture already in
the database (e.g. before a restart) isn't repeated.
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from app.db.session import AsyncSessionLocal
from app.models.fo_scan import MARK_0920, MARK_CLOSE, MARK_PRE_OPEN
from app.services.backfill_platform.coverage import IST, is_trading_day, previous_trading_day
from app.services.fo_scan import oi_store
from app.services.options.pcr_snapshot_scheduler import kite_broker

logger = logging.getLogger(__name__)

TICK_SECONDS = 5
RETRY = timedelta(seconds=60)
NOT_LOGGED_IN = "Zerodha isn't logged in -- OI readings resume after the daily login"

# mark -> (window start, window end)
WINDOWS = {
    MARK_PRE_OPEN: (time(9, 10), time(9, 14, 30)),
    MARK_0920: (time(9, 20, 15), time(9, 24, 30)),
    MARK_CLOSE: (time(15, 31), time(17, 0)),
}


class FoOiStoreScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._done: set[tuple[date, str]] = set()
        self._last_try: dict[tuple[date, str], datetime] = {}
        self.last_capture: dict | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick(datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("F&O OI store tick failed")
                self.last_error = "capture failed, see logs"
            await asyncio.sleep(TICK_SECONDS)

    def due(self, now: datetime) -> str | None:
        """The mark whose window `now` falls in and isn't done or waiting
        out its retry, if any."""
        now_ist = now.astimezone(IST)
        session = now_ist.date()
        if not is_trading_day(session):
            return None
        for mark, (start, end) in WINDOWS.items():
            if start <= now_ist.time() < end and (session, mark) not in self._done:
                last = self._last_try.get((session, mark))
                if last is None or now - last >= RETRY:
                    return mark
        return None

    async def tick(self, now: datetime) -> None:
        mark = self.due(now)
        if mark is not None:
            await self.run_capture(now.astimezone(IST).date(), mark, now)

    async def run_capture(self, session: date, mark: str, now: datetime) -> bool:
        self._last_try[(session, mark)] = now
        async with AsyncSessionLocal() as db:
            if mark == MARK_PRE_OPEN and await oi_store.has_reading(db, previous_trading_day(session), MARK_CLOSE):
                self._done.add((session, mark))  # yesterday's close is there: no backup needed
                return True
            if await oi_store.has_reading(db, session, mark):
                self._done.add((session, mark))
                return True
            broker = await kite_broker(db)
            if broker is None:
                self.last_error = NOT_LOGGED_IN
                return False
            contracts = await oi_store.stock_contracts(db, session, next_month_on_expiry=(mark == MARK_CLOSE))
            result = await oi_store.capture(db, broker, session, mark, contracts, now=datetime.now(timezone.utc))
            await oi_store.prune(db, session)
        self.last_capture = {"session": session.isoformat(), "mark": mark, **result}
        if result["quoted"] == 0:
            self.last_error = result["error"] or "Kite returned no quotes"
            return False
        self._done = {k for k in self._done if k[0] >= previous_trading_day(session)} | {(session, mark)}
        self.last_error = result["error"]
        return True

    def status(self) -> dict:
        return {"running": self._task is not None, "last_capture": self.last_capture, "last_error": self.last_error}


fo_oi_store_scheduler = FoOiStoreScheduler()
