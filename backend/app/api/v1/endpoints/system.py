from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.broker.registry import get_broker_adapter

router = APIRouter()


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

    overall = "healthy" if all(v == "healthy" for v in components.values()) else "degraded"

    return {"status": overall, "components": components}
