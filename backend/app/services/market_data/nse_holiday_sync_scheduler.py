"""Periodically refreshes the live NSE holiday calendar (nse_holidays.py)
from NSE's own public API. A holiday calendar changes at most once a year
(published each December) and never intraday, so this runs far less often
than the other market-data schedulers -- once a day is already generous,
just enough to pick up NSE's next-year calendar shortly after it's
published, or recover automatically once whatever was blocking the fetch
(NSE's own bot-mitigation, a transient outage) clears.

Mirrors the shape of the other background schedulers in this app
(PaperTradingScheduler, CatalogSyncScheduler): a periodic asyncio task,
started/stopped from main.py's lifespan.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.services.market_data.nse_holidays import refresh_from_nse

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 24 * 60 * 60


class NseHolidaySyncScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_run_at: datetime | None = None
        self.last_synced_count: int = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None

    async def _run(self) -> None:
        # Refresh once immediately on startup -- otherwise a freshly
        # started process relies on STATIC_HOLIDAYS alone for a full day
        # before its first scheduled refresh.
        await self._tick()
        while True:
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
            await self._tick()

    async def _tick(self) -> None:
        try:
            self.last_synced_count = await refresh_from_nse()
            self.last_run_at = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            raise
        except Exception:
            # refresh_from_nse() already catches and records its own
            # failures in nse_holidays.last_live_fetch_error without
            # raising -- this is only a backstop against a genuinely
            # unexpected bug in the scheduling loop itself.
            logger.exception("NSE holiday sync tick failed unexpectedly")


nse_holiday_sync_scheduler = NseHolidaySyncScheduler()
