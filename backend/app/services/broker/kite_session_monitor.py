"""Proactively detects when a CONNECTED Zerodha Kite session has actually
died -- Kite's access_token expires daily (~6am IST) with no refresh token
(see zerodha_broker.py), and BrokerConnection.status otherwise only updates
the next time something else happens to call the adapter and fails, which
could be hours after expiry or never if nothing tries in the meantime.
Runs a lightweight GET /user/profile (via authenticate()'s own access_token
validation path) against every account this app currently believes is
CONNECTED, and flips it to ERROR the moment that call fails -- so
Settings > Brokers shows "Login with Zerodha" again immediately instead of
a stale green badge.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.encryption import decrypt_payload
from app.db.session import AsyncSessionLocal
from app.models.broker import Broker, BrokerAccount, BrokerConnection, ConnectionStatus
from app.services.broker.zerodha_broker import ZerodhaKiteBroker

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 900


class KiteSessionMonitorScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_check_at: datetime | None = None
        self.last_checked_count: int = 0
        self.last_error: str | None = None

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
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            try:
                self.last_checked_count = await self.check_once()
                self.last_check_at = datetime.now(timezone.utc)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Kite session check tick failed")
                self.last_error = str(exc)

    async def check_once(self) -> int:
        async with AsyncSessionLocal() as db:
            return await self.check(db)

    async def check(self, db: AsyncSession) -> int:
        """The checkable core, taking an explicit session -- split out from
        check_once() so tests can exercise it against their own isolated
        session instead of the module-level AsyncSessionLocal."""
        accounts = (
            await db.execute(
                select(BrokerAccount)
                .join(Broker, Broker.id == BrokerAccount.broker_id)
                .join(BrokerConnection, BrokerConnection.broker_account_id == BrokerAccount.id)
                .options(selectinload(BrokerAccount.credential), selectinload(BrokerAccount.connection))
                .where(Broker.code == "zerodha_kite", BrokerConnection.status == ConnectionStatus.CONNECTED.value)
            )
        ).scalars().all()

        checked = 0
        for account in accounts:
            if account.credential is None or account.connection is None:
                continue
            creds = json.loads(decrypt_payload(account.credential.encrypted_payload))
            broker = ZerodhaKiteBroker()
            try:
                await broker.authenticate(creds)
                account.connection.last_heartbeat_at = datetime.now(timezone.utc)
                account.connection.last_error = None
            except Exception as exc:
                account.connection.status = ConnectionStatus.ERROR.value
                account.connection.last_error = str(exc)
            checked += 1
        await db.commit()
        return checked


kite_session_monitor_scheduler = KiteSessionMonitorScheduler()
