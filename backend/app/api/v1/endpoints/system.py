from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.backfill_platform.nfo_expiry_rotation import nfo_expiry_rotation_scheduler
from app.services.broker.kite_ticker_service import diagnose_zerodha_connection, kite_ticker_service
from app.services.broker.registry import get_broker_adapter
from app.services.monitoring.service import get_application_metrics, get_infra_metrics, get_trading_metrics

router = APIRouter()

# Core components: an error here means TradingMaster itself is unhealthy and
# drives the overall status. Optional external data sources are reported
# too (PRD section 30), but one being unreachable is a normal, expected
# state rather than a platform fault, so it's excluded from the overall
# rollup.
CORE_COMPONENTS = ("database", "broker_engine")


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    components: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        components["database"] = "healthy"
    except Exception:
        components["database"] = "error"

    try:
        adapter = get_broker_adapter("zerodha_kite")
        await adapter.connect()
        await adapter.disconnect()
        components["broker_engine"] = "healthy"
    except Exception:
        components["broker_engine"] = "error"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://api.india.delta.exchange/v2/products", params={"page_size": "1"})
        components["market_data_delta"] = "healthy" if resp.status_code == 200 else "unreachable"
    except Exception:
        components["market_data_delta"] = "unreachable"

    # Non-core, same as market_data_delta above -- the platform is fine
    # without a live NFO/NSE stream (F&O/equities just fall back to
    # simulated prices until the next reconnect cycle finds a connected
    # account), so this never drives the overall rollup.
    if kite_ticker_service._ticker is not None and kite_ticker_service._ticker.is_connected():
        components["kite_ticker"] = "connected"
    elif kite_ticker_service.last_error:
        components["kite_ticker"] = f"error: {kite_ticker_service.last_error}"
    else:
        components["kite_ticker"] = "connecting"

    overall = "healthy" if all(components[c] == "healthy" for c in CORE_COMPONENTS) else "degraded"

    # Secret-free breakdown of exactly which step is failing when Settings
    # shows a connected Zerodha account but the ticker/rotation schedulers
    # still report "No connected Zerodha account" -- see
    # diagnose_zerodha_connection's own docstring for why this exists.
    # Counts and booleans only, never a credential value -- safe on this
    # public, unauthenticated endpoint.
    kite_diagnostic = await diagnose_zerodha_connection(db)
    kite_diagnostic["nfo_expiry_rotation_last_run_at"] = (
        nfo_expiry_rotation_scheduler.last_run_at.isoformat() if nfo_expiry_rotation_scheduler.last_run_at else None
    )
    kite_diagnostic["nfo_expiry_rotation_last_error"] = nfo_expiry_rotation_scheduler.last_error
    kite_diagnostic["nfo_expiry_rotation_last_added_count"] = nfo_expiry_rotation_scheduler.last_added_count

    return {"status": overall, "components": components, "kite_diagnostic": kite_diagnostic}


@router.get("/monitor")
async def monitor(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> dict[str, Any]:
    """PRD section 37: infrastructure, application, and trading metrics --
    every number here is real (psutil for infra, live DB counts for
    trading), not a placeholder."""
    return {
        "infrastructure": asdict(get_infra_metrics()),
        "application": asdict(get_application_metrics()),
        "trading": asdict(await get_trading_metrics(db)),
    }
