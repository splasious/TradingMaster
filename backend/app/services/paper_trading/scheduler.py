import asyncio
import logging

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.paper_trading import DeploymentStatus, PaperDeployment
from app.services.market_data.seed_price import get_seed_price
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.engine import evaluate_deployment

logger = logging.getLogger(__name__)

EVALUATION_INTERVAL_SECONDS = 10


class PaperTradingScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

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
            try:
                async with AsyncSessionLocal() as db:
                    result = await db.execute(select(PaperDeployment).where(PaperDeployment.status == DeploymentStatus.ACTIVE.value))
                    deployments = list(result.scalars().all())
                    for deployment in deployments:
                        seed_price = tick_engine.get_current_price(deployment.instrument_id)
                        if seed_price is None:
                            seed_price = await get_seed_price(db, deployment.instrument_id)
                        tick_engine.subscribe(deployment.instrument_id, seed_price=seed_price)
                        try:
                            await evaluate_deployment(db, deployment)
                        except Exception:
                            logger.exception("Paper deployment %s evaluation failed", deployment.id)
            except Exception:
                logger.exception("Paper trading scheduler tick failed")


paper_trading_scheduler = PaperTradingScheduler()
