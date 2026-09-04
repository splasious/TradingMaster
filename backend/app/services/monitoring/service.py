"""System monitoring (PRD section 37): real infrastructure metrics via
psutil, real counts of what the app is actually doing -- nothing here is a
placeholder or a fabricated number.
"""

import os
import time
from dataclasses import dataclass

import psutil
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.live_trading import LiveDeployment
from app.models.paper_trading import PaperDeployment
from app.services.market_data.active_timeframe_sync_scheduler import active_timeframe_sync_scheduler
from app.services.market_data.real_price_feed import real_price_feed
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.scheduler import paper_trading_scheduler

_PROCESS_START = time.monotonic()


@dataclass
class InfraMetrics:
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float


@dataclass
class ApplicationMetrics:
    uptime_seconds: float
    tick_engine_running: bool
    tick_engine_subscribed_instruments: int
    paper_trading_scheduler_running: bool
    real_price_feed_running: bool
    active_timeframe_sync_scheduler_running: bool


@dataclass
class TradingMetrics:
    active_paper_deployments: int
    active_live_deployments: int


def get_infra_metrics() -> InfraMetrics:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.getcwd())  # "/" isn't a valid path on Windows; cwd always resolves cross-platform
    return InfraMetrics(
        cpu_percent=psutil.cpu_percent(interval=0.1),
        memory_percent=memory.percent,
        memory_used_mb=round(memory.used / (1024**2), 1),
        memory_total_mb=round(memory.total / (1024**2), 1),
        disk_percent=disk.percent,
        disk_used_gb=round(disk.used / (1024**3), 2),
        disk_total_gb=round(disk.total / (1024**3), 2),
    )


def get_application_metrics() -> ApplicationMetrics:
    return ApplicationMetrics(
        uptime_seconds=round(time.monotonic() - _PROCESS_START, 1),
        tick_engine_running=tick_engine._task is not None,
        tick_engine_subscribed_instruments=sum(1 for c in tick_engine._subscriber_counts.values() if c > 0),
        paper_trading_scheduler_running=paper_trading_scheduler._task is not None,
        real_price_feed_running=real_price_feed.running,
        active_timeframe_sync_scheduler_running=active_timeframe_sync_scheduler.running,
    )


async def get_trading_metrics(db: AsyncSession) -> TradingMetrics:
    paper_count = (
        await db.execute(select(func.count()).select_from(PaperDeployment).where(PaperDeployment.status == "active"))
    ).scalar_one()
    live_count = (
        await db.execute(select(func.count()).select_from(LiveDeployment).where(LiveDeployment.status == "active"))
    ).scalar_one()
    return TradingMetrics(active_paper_deployments=paper_count, active_live_deployments=live_count)
