"""Runs native strategies live (native_live.py) while NSE is open: each
active one every EVALUATION_INTERVAL_SECONDS, the account-wide loss limit
before them, and every RECONCILE_INTERVAL_SECONDS each broker account's
holdings against what its strategies hold. A pause that lifts by itself
(the daily loss limit, resume_at) lifts at the next session's open.

Logged-in brokers are kept for GATEWAY_TTL rather than logged into on every
check; one that fails is dropped and logged into again next time. A broker
that can't be logged into pauses the strategies on it -- their orders
couldn't be sent, and what they hold couldn't be checked.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.broker import Broker, BrokerAccount
from app.models.live_native import LIVE_NATIVE_ACTIVE, LIVE_NATIVE_PAUSED, LiveNativeDeployment
from app.services.live_trading.native_gateway import BrokerGateway
from app.services.live_trading.native_live import check_account_limits, pause, reconcile_account, run_live_native
from app.services.market_data.hours import nse_market_open

logger = logging.getLogger(__name__)

EVALUATION_INTERVAL_SECONDS = 10
RECONCILE_INTERVAL = timedelta(seconds=60)
GATEWAY_TTL = timedelta(minutes=10)

# One live check at a time: two checks of the same strategy must never send
# the same orders twice.
_live_lock = asyncio.Lock()


async def open_gateway(db: AsyncSession, broker_account_id: uuid.UUID) -> BrokerGateway:
    from app.services.live_trading.oms import get_authenticated_broker  # local import avoids a cycle

    account = await db.get(BrokerAccount, broker_account_id)
    if account is None:
        raise RuntimeError("broker account not found")
    broker_row = await db.get(Broker, account.broker_id)
    return BrokerGateway(broker_row.code, await get_authenticated_broker(db, account, for_live_strategies=True))


class LiveNativeScheduler:
    def __init__(self, gateway_factory=open_gateway) -> None:
        self._task: asyncio.Task | None = None
        self._gateways: dict[uuid.UUID, tuple[BrokerGateway, datetime]] = {}
        self._gateway_factory = gateway_factory
        self._last_reconcile: datetime | None = None
        self.last_tick_at: datetime | None = None
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
                async with AsyncSessionLocal() as db:
                    await self.tick(db, datetime.now(timezone.utc))
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Live native tick failed")
                self.last_error = str(exc)
            await asyncio.sleep(EVALUATION_INTERVAL_SECONDS)

    async def gateway(self, db: AsyncSession, broker_account_id: uuid.UUID, now: datetime) -> BrokerGateway:
        cached = self._gateways.get(broker_account_id)
        if cached is not None and now - cached[1] < GATEWAY_TTL:
            return cached[0]
        gateway = await self._gateway_factory(db, broker_account_id)
        self._gateways[broker_account_id] = (gateway, now)
        return gateway

    def forget_gateway(self, broker_account_id: uuid.UUID) -> None:
        self._gateways.pop(broker_account_id, None)

    async def tick(self, db: AsyncSession, now: datetime) -> int:
        if not nse_market_open(now):
            return 0
        async with _live_lock:
            self.last_tick_at = now
            await self._resume_due(db, now)

            async def gateway_for(deployment: LiveNativeDeployment) -> BrokerGateway:
                return await self.gateway(db, deployment.broker_account_id, now)

            await check_account_limits(db, gateway_for, now)

            deployments = (await db.execute(
                select(LiveNativeDeployment).where(LiveNativeDeployment.status == LIVE_NATIVE_ACTIVE)
            )).scalars().all()
            ran = 0
            for deployment in deployments:
                try:
                    gateway = await self.gateway(db, deployment.broker_account_id, now)
                except Exception as exc:
                    self.forget_gateway(deployment.broker_account_id)
                    await pause(db, deployment, f"Couldn't log in to the broker ({exc}) -- paused; log in again, then resume", now)
                    await db.commit()
                    continue
                try:
                    await run_live_native(db, deployment, gateway, now)
                    ran += 1
                except Exception:
                    logger.exception("Live native deployment %s failed", deployment.id)
                    self.forget_gateway(deployment.broker_account_id)
                    await db.rollback()

            if self._last_reconcile is None or now - self._last_reconcile >= RECONCILE_INTERVAL:
                self._last_reconcile = now
                for account_id in {d.broker_account_id for d in deployments}:
                    try:
                        gateway = await self.gateway(db, account_id, now)
                        await reconcile_account(db, account_id, gateway, now)
                    except Exception:
                        logger.exception("Reconciling broker account %s failed", account_id)
                        self.forget_gateway(account_id)
            return ran

    async def _resume_due(self, db: AsyncSession, now: datetime) -> None:
        due = (await db.execute(
            select(LiveNativeDeployment).where(
                LiveNativeDeployment.status == LIVE_NATIVE_PAUSED, LiveNativeDeployment.resume_at.is_not(None),
                LiveNativeDeployment.resume_at <= now,
            )
        )).scalars().all()
        for deployment in due:
            deployment.status = LIVE_NATIVE_ACTIVE
            deployment.pause_reason = None
            deployment.paused_at = None
            deployment.resume_at = None
        if due:
            await db.commit()


live_native_scheduler = LiveNativeScheduler()
