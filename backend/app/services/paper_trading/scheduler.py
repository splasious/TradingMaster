import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.paper_trading import DeploymentStatus, PaperDeployment
from app.services.market_data.seed_price import get_seed_price
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.engine import evaluate_deployment

logger = logging.getLogger(__name__)

EVALUATION_INTERVAL_SECONDS = 10


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
        self.last_tick_started_at: datetime | None = None
        self.last_tick_completed_at: datetime | None = None
        self.last_tick_evaluated_count: int = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(EVALUATION_INTERVAL_SECONDS)
            self.last_tick_started_at = datetime.now(timezone.utc)
            try:
                async with AsyncSessionLocal() as db:
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
                    self.last_tick_evaluated_count = evaluated
            except Exception:
                logger.exception("Paper trading scheduler tick failed")
            finally:
                self.last_tick_completed_at = datetime.now(timezone.utc)


paper_trading_scheduler = PaperTradingScheduler()
