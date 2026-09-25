"""Runs services/options/pcr_snapshots.py: a live capture at every 15-minute
mark from 09:00 to 15:30 IST on trading days, and a gap fill every
FILL_EVERY (when anything is missing) for marks that weren't captured --
e.g. before the day's Zerodha login -- so the records stay continuous.

Checks every TICK_SECONDS, so a mark is captured within a few seconds of
it; a failed capture is retried on each tick until CAPTURE_GRACE runs out.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.db.session import AsyncSessionLocal
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker
from app.services.options.pcr_snapshots import UNDERLYINGS, capture_live, due_capture_mark, fill_gaps, missing_marks

logger = logging.getLogger(__name__)

TICK_SECONDS = 5
FILL_EVERY = timedelta(minutes=15)
NOT_LOGGED_IN = "Zerodha isn't logged in -- records resume after the daily login"


async def kite_broker(db) -> ZerodhaKiteBroker | None:
    creds = await find_connected_zerodha_credentials(db)
    if creds is None:
        return None
    broker = ZerodhaKiteBroker()
    broker._api_key = creds["api_key"]
    broker._access_token = creds["access_token"]
    return broker


class PcrSnapshotScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._fill_task: asyncio.Task | None = None
        self._captured: set[datetime] = set()
        self._last_fill_try: datetime | None = None
        self.last_capture_ts: datetime | None = None
        self.last_capture_at: datetime | None = None
        self.last_error: str | None = None
        self.last_fill_at: datetime | None = None
        self.last_fill_result: dict | None = None
        self.last_fill_error: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        for task in (self._task, self._fill_task):
            if task is not None:
                task.cancel()
        self._task = None
        self._fill_task = None

    @property
    def running(self) -> bool:
        return self._task is not None

    @property
    def filling(self) -> bool:
        return self._fill_task is not None and not self._fill_task.done()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick(datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PCR snapshot tick failed")
                self.last_error = "capture failed, see logs"
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: datetime) -> None:
        mark = due_capture_mark(now)
        if mark is not None and mark not in self._captured:
            await self.capture(mark, now)
            return
        if not self.filling and (self._last_fill_try is None or now - self._last_fill_try >= FILL_EVERY):
            self._last_fill_try = now
            self._fill_task = asyncio.create_task(self.fill(now))

    async def capture(self, mark: datetime, now: datetime) -> bool:
        async with AsyncSessionLocal() as db:
            broker = await kite_broker(db)
            if broker is None:
                self.last_error = NOT_LOGGED_IN
                return False
            for underlying in UNDERLYINGS:
                try:
                    await capture_live(db, broker, underlying, mark)
                except KiteAPIError as exc:
                    self.last_error = str(exc)
                    return False
        self._captured = {m for m in self._captured if now - m < timedelta(days=2)} | {mark}
        self.last_capture_ts = mark
        self.last_capture_at = datetime.now(timezone.utc)
        self.last_error = None
        return True

    async def fill(self, now: datetime) -> None:
        try:
            async with AsyncSessionLocal() as db:
                results = {}
                for underlying in UNDERLYINGS:
                    if not await missing_marks(db, underlying, now):
                        results[underlying] = {"filled": 0, "unfillable": 0, "missing": 0}
                        continue
                    results[underlying] = await fill_gaps(db, await kite_broker(db), underlying, now)
            self.last_fill_result = results
            waiting = any(r.get("waiting_login") for r in results.values())
            self.last_fill_error = NOT_LOGGED_IN if waiting else None
            self.last_fill_at = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            raise
        except KiteAPIError as exc:
            self.last_fill_error = str(exc)
        except Exception:
            logger.exception("PCR gap fill failed")
            self.last_fill_error = "gap fill failed, see logs"

    def status(self) -> dict:
        return {
            "running": self.running,
            "filling": self.filling,
            "last_capture_ts": self.last_capture_ts,
            "last_capture_at": self.last_capture_at,
            "last_error": self.last_error,
            "last_fill_at": self.last_fill_at,
            "last_fill_result": self.last_fill_result,
            "last_fill_error": self.last_fill_error,
        }


pcr_snapshot_scheduler = PcrSnapshotScheduler()
