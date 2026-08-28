import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.market_data import BackfillJob, BackfillStatus, OhlcvCandle
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.registry import get_market_data_source


async def run_backfill_job(job_id: uuid.UUID) -> None:
    """Runs as a FastAPI BackgroundTask -- after the HTTP response is sent,
    outside the request's DB session, so it opens its own (PRD section 10:
    a backfill is a real async job with trackable progress/status, not a
    blocking request)."""
    async with AsyncSessionLocal() as db:
        job = await db.get(BackfillJob, job_id)
        if job is None:
            return
        instrument = await db.get(Instrument, job.instrument_id)
        if instrument is None:
            job.status = BackfillStatus.FAILED.value
            job.error_message = "Instrument no longer exists"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        job.status = BackfillStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            source = get_market_data_source(instrument.data_source)
            bars = await source.get_historical_data(instrument.external_ref, job.timeframe, None, None)
        except MarketDataSourceError as exc:
            job.status = BackfillStatus.FAILED.value
            job.error_message = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            return

        job.downloaded_count = len(bars)

        existing_result = await db.execute(
            select(OhlcvCandle.ts).where(
                OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == job.timeframe
            )
        )
        # SQLite doesn't preserve tzinfo on DateTime(timezone=True) columns,
        # so timestamps read back come out naive even though they were
        # always UTC -- normalize before comparing against timezone-aware
        # bar timestamps, or every bar looks "new" on every rerun.
        existing_ts = {as_aware_utc(row[0]) for row in existing_result.all()}

        inserted = 0
        duplicates = 0
        for bar in bars:
            bar_ts = as_aware_utc(bar["ts"])
            if bar_ts in existing_ts:
                duplicates += 1
                continue
            db.add(
                OhlcvCandle(
                    instrument_id=instrument.id,
                    timeframe=job.timeframe,
                    ts=bar_ts,
                    open=bar["open"],
                    high=bar["high"],
                    low=bar["low"],
                    close=bar["close"],
                    volume=bar.get("volume"),
                    source=instrument.data_source,
                )
            )
            existing_ts.add(bar_ts)
            inserted += 1

        job.inserted_count = inserted
        job.duplicate_count = duplicates
        job.status = BackfillStatus.COMPLETED.value
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
