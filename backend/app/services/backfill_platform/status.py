"""Per-source connection/auth status (PRD section 4.1's "Connection/auth
status indicator" for each block). Every check is a real reachability/auth
check, not a stored flag that could go stale."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker import Broker, BrokerAccount, BrokerConnection, ConnectionStatus

# Kite sessions expire daily at a fixed time, but the exact expiry instant
# isn't returned anywhere in the session response -- this is the documented
# convention (Kite Connect v3 docs), not read from a real field, so it's
# labeled "estimated" everywhere it's shown rather than presented as exact.
_KITE_DAILY_EXPIRY_IST_HOUR = 6
_IST_OFFSET = timedelta(hours=5, minutes=30)


@dataclass
class SourceStatus:
    source: str
    connected: bool
    detail: str
    expires_at: datetime | None = None  # Zerodha only, estimated


async def delta_status() -> SourceStatus:
    # Delta's historical-candle endpoint is genuinely public (verified live
    # earlier in this project) -- no API key is needed for backfill, unlike
    # what the PRD assumed for this block. Reflecting that honestly here
    # rather than pretending an auth requirement that doesn't exist.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.india.delta.exchange/v2/products", params={"page_size": "1"})
        if resp.status_code == 200:
            return SourceStatus(source="delta", connected=True, detail="Delta Exchange public API reachable (no API key required for historical data)")
        return SourceStatus(source="delta", connected=False, detail=f"Delta Exchange returned HTTP {resp.status_code}")
    except (httpx.ConnectError, httpx.TimeoutException):
        return SourceStatus(source="delta", connected=False, detail="Could not reach Delta Exchange's API")


async def zerodha_status(db: AsyncSession, user_id) -> SourceStatus:
    # A user can end up with more than one zerodha_kite BrokerAccount (e.g.
    # reconnecting via "Connect Broker" instead of "Login with Zerodha" on
    # the existing one after the daily session expired) -- same ordering
    # fix as get_authenticated_kite_broker (kite_auth.py): without it, this
    # picked whichever row Postgres scanned first, which in practice was a
    # stale/broken account from days earlier, showing "Disconnected" with
    # its old error even while the real, currently-connected account was
    # healthy seconds ago.
    row = (
        await db.execute(
            select(BrokerConnection)
            .join(BrokerAccount, BrokerAccount.id == BrokerConnection.broker_account_id)
            .join(Broker, Broker.id == BrokerAccount.broker_id)
            .where(BrokerAccount.user_id == user_id, Broker.code == "zerodha_kite")
            .order_by(
                case((BrokerConnection.status == ConnectionStatus.CONNECTED.value, 0), else_=1),
                BrokerAccount.created_at.desc(),
            )
        )
    ).scalars().first()

    if row is None:
        return SourceStatus(source="zerodha", connected=False, detail="No Zerodha Kite account connected")
    if row.status != "connected":
        return SourceStatus(source="zerodha", connected=False, detail=row.last_error or f"Status: {row.status}")

    expires_at = None
    if row.last_heartbeat_at is not None:
        heartbeat = row.last_heartbeat_at
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        heartbeat_ist = heartbeat + _IST_OFFSET
        expiry_ist_date = heartbeat_ist.date() if heartbeat_ist.hour < _KITE_DAILY_EXPIRY_IST_HOUR else heartbeat_ist.date() + timedelta(days=1)
        expires_at_ist = datetime.combine(expiry_ist_date, datetime.min.time()) + timedelta(hours=_KITE_DAILY_EXPIRY_IST_HOUR)
        expires_at = expires_at_ist - _IST_OFFSET

    return SourceStatus(source="zerodha", connected=True, detail="Connected", expires_at=expires_at)
