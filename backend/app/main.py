import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.services.backfill_platform.catalog_sync_scheduler import catalog_sync_scheduler
from app.services.backfill_platform.jobs import fail_orphaned_jobs_on_startup
from app.services.backfill_platform.live_sync_scheduler import bf_live_sync_scheduler
from app.services.market_data.active_timeframe_sync_scheduler import active_timeframe_sync_scheduler
from app.services.market_data.real_price_feed import real_price_feed
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.scheduler import paper_trading_scheduler

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    orphaned_count = await fail_orphaned_jobs_on_startup()
    if orphaned_count:
        logger.warning("Marked %d backfill job(s) as failed -- orphaned by a previous server restart", orphaned_count)
    tick_engine.start()
    real_price_feed.start()
    paper_trading_scheduler.start()
    bf_live_sync_scheduler.start()
    catalog_sync_scheduler.start()
    active_timeframe_sync_scheduler.start()
    yield
    active_timeframe_sync_scheduler.stop()
    catalog_sync_scheduler.stop()
    bf_live_sync_scheduler.stop()
    paper_trading_scheduler.stop()
    real_price_feed.stop()
    tick_engine.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "status": "running"}
