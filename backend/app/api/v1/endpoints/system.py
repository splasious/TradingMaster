from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.broker.registry import get_broker_adapter
from app.services.monitoring.service import get_application_metrics, get_infra_metrics, get_trading_metrics

router = APIRouter()
settings = get_settings()

# Core components: an error here means TradingMaster itself is unhealthy and
# drives the overall status. Optional external data sources are reported
# too (PRD section 30), but one being unreachable is a normal, expected
# state (e.g. the local nse-yahoo-data sidecar simply isn't running) rather
# than a platform fault, so it's excluded from the overall rollup.
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
            resp = await client.get(f"{settings.yahoo_data_service_url}/health")
        components["market_data_yahoo_nse"] = "healthy" if resp.status_code == 200 else "unreachable"
    except Exception:
        components["market_data_yahoo_nse"] = "unreachable"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("https://api.india.delta.exchange/v2/products", params={"page_size": "1"})
        components["market_data_delta"] = "healthy" if resp.status_code == 200 else "unreachable"
    except Exception:
        components["market_data_delta"] = "unreachable"

    overall = "healthy" if all(components[c] == "healthy" for c in CORE_COMPONENTS) else "degraded"

    return {"status": overall, "components": components}


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
