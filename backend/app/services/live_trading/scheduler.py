"""Continuously re-evaluates every ACTIVE live deployment, the same way
paper_trading/scheduler.py already does for paper ones -- previously
`evaluate_live_deployment` only ever ran when a human clicked "Evaluate
Now" in the UI (`POST /live-trading/deployments/{id}/evaluate`), meaning a
live position's stop-loss/take-profit and entry/exit signal only updated
on a manual click. Before real capital depends on this, that has to be
automatic, not something a person has to remember to keep clicking.

Same `EVALUATION_INTERVAL_SECONDS` as paper trading -- matches the
frontend's own `last_evaluated_at`-older-than-60s "stale" indicator
(live-trading/page.tsx), which already assumed something under a minute.
No tick_engine subscribe/seed step (unlike paper trading): live trading
always prices off the real broker (Delta ticker / Kite LTP), never the
simulated engine.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.live_trading import LiveDeployment
from app.services.live_trading.oms import evaluate_live_deployment

logger = logging.getLogger(__name__)

EVALUATION_INTERVAL_SECONDS = 10


class LiveTradingScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

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
        while True:
            await asyncio.sleep(EVALUATION_INTERVAL_SECONDS)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Live trading scheduler tick failed")

    async def run_once(self) -> int:
        async with AsyncSessionLocal() as db:
            return await self.run(db)

    async def run(self, db: AsyncSession) -> int:
        """The evaluable core, taking an explicit session -- split out
        from run_once() so tests can exercise a single tick against their
        own isolated session instead of the module-level AsyncSessionLocal,
        the same pattern active_timeframe_sync_scheduler.py/
        kite_ticker_service.py already use."""
        result = await db.execute(select(LiveDeployment).where(LiveDeployment.status == "active"))
        deployments = list(result.scalars().all())
        evaluated = 0
        for deployment in deployments:
            try:
                await evaluate_live_deployment(db, deployment)
                evaluated += 1
            except Exception:
                logger.exception("Live deployment %s evaluation failed", deployment.id)
        return evaluated


live_trading_scheduler = LiveTradingScheduler()
