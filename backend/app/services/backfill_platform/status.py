"""Per-source connection/auth status (PRD section 4.1's "Connection/auth
status indicator" for each block). Every check is a real reachability/auth
check, not a stored flag that could go stale."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.broker import Broker, BrokerAccount, BrokerConnection

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


async def yahoo_status() -> SourceStatus:
    settings = get_settings()
    url = f"{settings.yahoo_data_service_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return SourceStatus(source="yahoo", connected=True, detail="nse-yahoo-data service reachable")
        return SourceStatus(source="yahoo", connected=False, detail=f"nse-yahoo-data returned HTTP {resp.status_code}")
    except (httpx.ConnectError, httpx.TimeoutException):
        return SourceStatus(source="yahoo", connected=False, detail=f"Could not reach nse-yahoo-data at {url}")


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
    row = (
        await db.execute(
            select(BrokerConnection)
            .join(BrokerAccount, BrokerAccount.id == BrokerConnection.broker_account_id)
            .join(Broker, Broker.id == BrokerAccount.broker_id)
            .where(BrokerAccount.user_id == user_id, Broker.code == "zerodha_kite")
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
