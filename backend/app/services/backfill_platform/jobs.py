"""Runs a Data Backfill Platform job in the background, mirroring
services/market_data/backfill.py's pattern (own DB session, real
source calls, real dedup) but source-scoped rather than instrument-catalog-
scoped, and honoring the requested date range (PRD 4.1's From/To pickers --
the main platform's backfill never exposed this)."""

import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfOhlcvBar, BfSymbol
from app.services.backfill_platform.kite_auth import get_authenticated_kite_broker
from app.services.broker.zerodha_broker import KiteAPIError
from app.services.market_data.base import Bar, MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.market_data.yahoo_source import YahooNSEDataSource


def _to_datetime(d: date | None, end_of_day: bool = False) -> datetime | None:
    if d is None:
        return None
    t = time(23, 59, 59) if end_of_day else time(0, 0, 0)
    return datetime.combine(d, t, tzinfo=timezone.utc)


async def _fetch_bars(db: AsyncSession, source: str, symbol: str, timeframe: str, start: datetime | None, end: datetime | None, user_id) -> list[Bar]:
    if source == "yahoo":
        return await YahooNSEDataSource().get_historical_data(symbol, timeframe, start, end)
    if source == "delta":
        return await DeltaExchangeDataSource().get_historical_data(symbol, timeframe, start, end)
    if source == "zerodha":
        try:
            broker = await get_authenticated_kite_broker(db, user_id)
            return await broker.get_historical_data(symbol, timeframe, start, end)  # type: ignore[return-value]
        except KiteAPIError as exc:
            raise MarketDataSourceError(str(exc)) from exc
    raise MarketDataSourceError(f"Unknown source '{source}'")


async def run_bf_backfill_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(BfBackfillJob, job_id)
        if job is None:
            return
        symbol_row = await db.get(BfSymbol, job.symbol_id)
        if symbol_row is None:
            job.status = BfBackfillStatus.FAILED.value
            job.error_message = "Symbol no longer exists"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        job.status = BfBackfillStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        start = _to_datetime(job.start_date)
        end = _to_datetime(job.end_date, end_of_day=True)

        try:
            bars = await _fetch_bars(db, job.source, symbol_row.symbol, job.timeframe, start, end, job.requested_by)
        except MarketDataSourceError as exc:
            job.status = BfBackfillStatus.FAILED.value
            job.error_message = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        job.downloaded_count = len(bars)

        existing_result = await db.execute(
            select(BfOhlcvBar.ts).where(BfOhlcvBar.symbol_id == symbol_row.id, BfOhlcvBar.timeframe == job.timeframe)
        )
        # SQLite doesn't preserve tzinfo on DateTime(timezone=True) columns
        # (see services/market_data/backfill.py's identical comment) --
        # normalize both sides before comparing or every bar looks "new".
        existing_ts = {as_aware_utc(ts) for ts in existing_result.scalars().all()}

        inserted = 0
        duplicates = 0
        for bar in bars:
            bar_ts = as_aware_utc(bar["ts"])
            if bar_ts in existing_ts:
                duplicates += 1
                continue
            db.add(
                BfOhlcvBar(
                    symbol_id=symbol_row.id, timeframe=job.timeframe, ts=bar_ts,
                    open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"], volume=bar.get("volume"),
                )
            )
            existing_ts.add(bar_ts)
            inserted += 1

        job.inserted_count = inserted
        job.duplicate_count = duplicates
        job.status = BfBackfillStatus.COMPLETED.value
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
