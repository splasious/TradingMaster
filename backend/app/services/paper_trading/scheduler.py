import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.paper_trading import DeploymentStatus, PaperDeployment, PaperNativeDeployment
from app.services.market_data.hours import nse_market_open
from app.services.market_data.seed_price import get_seed_price
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.engine import evaluate_deployment
from app.services.paper_trading.native_runner import run_native_strategy

logger = logging.getLogger(__name__)

EVALUATION_INTERVAL_SECONDS = 10
# How often due wake-ups (ctx.wake_at) are looked for: a strategy asking
# for 09:20:07 runs within this much of it.
WAKE_CHECK_SECONDS = 0.5


async def diagnose_evaluation_freshness(db: AsyncSession) -> dict:
    """How recently ACTIVE paper deployments have actually been evaluated,
    read straight from the DB (last_evaluated_at, set on every genuine
    evaluation attempt now -- see paper_trading/engine.py). With a large
    deployment count (680+), one 10s-interval tick can genuinely take
    longer than 10s to reach every deployment sequentially -- this
    distinguishes "the scheduler is really working through all of them,
    just slower than the nominal interval" from "something's stuck and
    nothing's running at all", which the scheduler being a live asyncio
    task alone doesn't answer."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(PaperDeployment.last_evaluated_at).where(PaperDeployment.status == DeploymentStatus.ACTIVE.value)
    )
    times = [as_aware_utc(t) if t is not None else None for (t,) in result.all()]
    ages_seconds = [(now - t).total_seconds() for t in times if t is not None]
    return {
        "active_deployments": len(times),
        "never_evaluated": sum(1 for t in times if t is None),
        "oldest_evaluation_age_seconds": round(max(ages_seconds), 1) if ages_seconds else None,
        "newest_evaluation_age_seconds": round(min(ages_seconds), 1) if ages_seconds else None,
    }


class PaperTradingScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake_task: asyncio.Task | None = None
        # Native deployment id -> when its strategy asked to run next.
        self._wakeups: dict[uuid.UUID, datetime] = {}
        self.last_tick_started_at: datetime | None = None
        self.last_tick_completed_at: datetime | None = None
        self.last_tick_evaluated_count: int = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        if self._wake_task is None:
            self._wake_task = asyncio.create_task(self._run_wakeups())

    def stop(self) -> None:
        for task in (self._task, self._wake_task):
            if task is not None:
                task.cancel()
        self._task = None
        self._wake_task = None

    def note_wakeup(self, deployment_id: uuid.UUID, wake_at: datetime | None) -> None:
        if wake_at is None:
            self._wakeups.pop(deployment_id, None)
        else:
            self._wakeups[deployment_id] = as_aware_utc(wake_at)

    async def _run_wakeups(self) -> None:
        while True:
            await asyncio.sleep(WAKE_CHECK_SECONDS)
            try:
                await self.run_due_wakeups(datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Native wake-up run failed")

    async def run_due_wakeups(self, now: datetime) -> int:
        """Runs every native deployment whose requested wake-up has come,
        on top of the regular cycle -- same market-hours gate."""
        due = [dep_id for dep_id, at in self._wakeups.items() if at <= now]
        if not due or not nse_market_open(now):
            return 0
        ran = 0
        async with AsyncSessionLocal() as db:
            for dep_id in due:
                self._wakeups.pop(dep_id, None)
                deployment = await db.get(PaperNativeDeployment, dep_id)
                if deployment is None or deployment.status != DeploymentStatus.ACTIVE.value:
                    continue
                try:
                    outcome = await run_native_strategy(db, deployment)
                    self.note_wakeup(dep_id, outcome.wake_at)
                    ran += 1
                except Exception:
                    logger.exception("Native paper deployment %s wake-up failed", dep_id)
        return ran

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(EVALUATION_INTERVAL_SECONDS)
            self.last_tick_started_at = datetime.now(timezone.utc)
            try:
                async with AsyncSessionLocal() as db:
                    self.last_tick_evaluated_count = await self.tick_once(db)
            except Exception:
                logger.exception("Paper trading scheduler tick failed")
            finally:
                self.last_tick_completed_at = datetime.now(timezone.utc)

    async def tick_once(self, db: AsyncSession, now: datetime | None = None) -> int:
        """A no-op outside real NSE trading hours (weekday 09:15-15:30 IST,
        see hours.nse_market_open) -- nothing genuinely changes while the
        exchange is shut, so evaluating anyway just means a strategy trading
        against whatever price TickEngine has cached from the last real
        session (get_price()/get_current_price() have no notion of "stale",
        see native_runner.py and tick_engine.py). That's exactly how a
        native strategy once opened and flat-closed a spread for an
        artifactual 0.00 P&L on a Saturday, against Friday's frozen price,
        with the scheduler itself never having any idea the exchange was
        closed all day."""
        if not nse_market_open(now or datetime.now(timezone.utc)):
            return 0

        result = await db.execute(select(PaperDeployment).where(PaperDeployment.status == DeploymentStatus.ACTIVE.value))
        deployments = list(result.scalars().all())
        evaluated = 0
        for deployment in deployments:
            seed_price = tick_engine.get_current_price(deployment.instrument_id)
            if seed_price is None:
                seed_price = await get_seed_price(db, deployment.instrument_id)
            tick_engine.subscribe(deployment.instrument_id, seed_price=seed_price)
            try:
                await evaluate_deployment(db, deployment)
                evaluated += 1
            except Exception:
                logger.exception("Paper deployment %s evaluation failed", deployment.id)

        native_result = await db.execute(
            select(PaperNativeDeployment).where(PaperNativeDeployment.status == DeploymentStatus.ACTIVE.value)
        )
        for native_deployment in native_result.scalars().all():
            try:
                outcome = await run_native_strategy(db, native_deployment)
                self.note_wakeup(native_deployment.id, outcome.wake_at)
                evaluated += 1
            except Exception:
                logger.exception("Native paper deployment %s evaluation failed", native_deployment.id)
        return evaluated


paper_trading_scheduler = PaperTradingScheduler()
