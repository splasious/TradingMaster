import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.services.backfill_platform.catalog_sync_scheduler import catalog_sync_scheduler
from app.services.backfill_platform.jobs import requeue_interrupted_jobs_on_startup
from app.services.backfill_platform.live_sync_scheduler import bf_live_sync_scheduler
from app.services.backfill_platform.nfo_expiry_rotation import nfo_expiry_rotation_scheduler
from app.services.backfill_platform.purge import timeframe_purge
from app.services.backfill_platform.topup import backfill_topup_scheduler
from app.services.backfill_platform.worker import backfill_worker
from app.services.broker.kite_session_monitor import kite_session_monitor_scheduler
from app.services.broker.kite_ticker_service import kite_ticker_service
from app.services.fo_scan.oi_store_scheduler import fo_oi_store_scheduler
from app.services.live_trading.scheduler import live_trading_scheduler
from app.services.market_data.active_timeframe_sync_scheduler import active_timeframe_sync_scheduler
from app.services.market_data.kite_rest_price_feed import kite_rest_price_feed
from app.services.market_data.nse_holiday_sync_scheduler import nse_holiday_sync_scheduler
from app.services.market_data.oi_snapshot_scheduler import oi_snapshot_scheduler
from app.services.market_data.real_price_feed import real_price_feed
from app.services.market_data.tick_engine import tick_engine
from app.services.options.pcr_snapshot_scheduler import pcr_snapshot_scheduler
from app.services.paper_trading.scheduler import paper_trading_scheduler

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    requeued = await requeue_interrupted_jobs_on_startup()
    if requeued:
        logger.warning("Put %d backfill job(s) interrupted by the last restart back in the queue", requeued)
    tick_engine.start()
    nse_holiday_sync_scheduler.start()
    real_price_feed.start()
    kite_rest_price_feed.start()
    paper_trading_scheduler.start()
    live_trading_scheduler.start()
    bf_live_sync_scheduler.start()
    backfill_worker.start()
    timeframe_purge.start()
    backfill_topup_scheduler.start()
    catalog_sync_scheduler.start()
    nfo_expiry_rotation_scheduler.start()
    active_timeframe_sync_scheduler.start()
    oi_snapshot_scheduler.start()
    pcr_snapshot_scheduler.start()
    fo_oi_store_scheduler.start()
    kite_session_monitor_scheduler.start()
    kite_ticker_service.start()
    yield
    kite_ticker_service.stop()
    kite_session_monitor_scheduler.stop()
    fo_oi_store_scheduler.stop()
    pcr_snapshot_scheduler.stop()
    oi_snapshot_scheduler.stop()
    active_timeframe_sync_scheduler.stop()
    nfo_expiry_rotation_scheduler.stop()
    catalog_sync_scheduler.stop()
    backfill_topup_scheduler.stop()
    timeframe_purge.stop()
    backfill_worker.stop()
    bf_live_sync_scheduler.stop()
    live_trading_scheduler.stop()
    paper_trading_scheduler.stop()
    kite_rest_price_feed.stop()
    real_price_feed.stop()
    nse_holiday_sync_scheduler.stop()
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
