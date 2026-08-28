import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.market_data import TIMEFRAMES, BackfillJob, OhlcvCandle
from app.models.user import User
from app.schemas.market_data import BackfillJobOut, BackfillRequest, CandleOut, QualityReportOut
from app.services.audit import write_audit_log
from app.services.market_data.backfill import run_backfill_job
from app.services.market_data.resample import resample_candles
from app.services.market_data.validation import compute_quality

router = APIRouter()


def _job_out(job: BackfillJob) -> BackfillJobOut:
    return BackfillJobOut(
        id=str(job.id),
        instrument_id=str(job.instrument_id),
        timeframe=job.timeframe,
        status=job.status,
        downloaded_count=job.downloaded_count,
        inserted_count=job.inserted_count,
        duplicate_count=job.duplicate_count,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


@router.post("/backfill", response_model=BackfillJobOut, status_code=status.HTTP_202_ACCEPTED)
async def start_backfill(
    payload: BackfillRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> BackfillJobOut:
    if payload.timeframe not in TIMEFRAMES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"timeframe must be one of {TIMEFRAMES}")

    instrument = await db.get(Instrument, uuid.UUID(payload.instrument_id))
    if instrument is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found")

    job = BackfillJob(instrument_id=instrument.id, timeframe=payload.timeframe, requested_by=user.id)
    db.add(job)
    await db.flush()

    await write_audit_log(
        db,
        user_id=user.id,
        action="BACKFILL_STARTED",
        object_type="instrument",
        object_id=str(instrument.id),
        new_value={"timeframe": payload.timeframe, "symbol": instrument.symbol},
    )
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_backfill_job, job.id)

    return _job_out(job)


@router.get("/backfill-jobs/{job_id}", response_model=BackfillJobOut)
async def get_backfill_job(
    job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> BackfillJobOut:
    job = await db.get(BackfillJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Backfill job not found")
    return _job_out(job)


@router.get("/backfill-jobs", response_model=list[BackfillJobOut])
async def list_backfill_jobs(
    instrument_id: str = Query(...), db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> list[BackfillJobOut]:
    result = await db.execute(
        select(BackfillJob)
        .where(BackfillJob.instrument_id == uuid.UUID(instrument_id))
        .order_by(BackfillJob.created_at.desc())
        .limit(20)
    )
    return [_job_out(j) for j in result.scalars().all()]


async def _load_candles(db: AsyncSession, instrument_id: str, timeframe: str) -> list[OhlcvCandle]:
    result = await db.execute(
        select(OhlcvCandle)
        .where(OhlcvCandle.instrument_id == uuid.UUID(instrument_id), OhlcvCandle.timeframe == timeframe)
        .order_by(OhlcvCandle.ts)
    )
    return list(result.scalars().all())


@router.get("/candles", response_model=list[CandleOut])
async def get_candles(
    instrument_id: str = Query(...),
    timeframe: str = Query(...),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CandleOut]:
    candles = await _load_candles(db, instrument_id, timeframe)
    if start:
        candles = [c for c in candles if c.ts >= start]
    if end:
        candles = [c for c in candles if c.ts <= end]
    return [CandleOut.model_validate(c, from_attributes=True) for c in candles]


@router.get("/candles/resampled", response_model=list[CandleOut])
async def get_resampled_candles(
    instrument_id: str = Query(...),
    base_timeframe: str = Query("1d"),
    target_timeframe: str = Query(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CandleOut]:
    """Higher-timeframe candles derived from stored `base_timeframe` data
    (PRD section 13: multi-timeframe engine). Never returns a still-forming
    period as if it were closed -- see resample.py."""
    candles = await _load_candles(db, instrument_id, base_timeframe)
    bars = [
        {"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
        for c in candles
    ]
    try:
        resampled = resample_candles(bars, target_timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return [CandleOut(**bar) for bar in resampled]


@router.get("/quality", response_model=QualityReportOut)
async def get_quality(
    instrument_id: str = Query(...),
    timeframe: str = Query(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> QualityReportOut:
    candles = await _load_candles(db, instrument_id, timeframe)
    report = compute_quality(candles, timeframe)
    return QualityReportOut(instrument_id=instrument_id, timeframe=timeframe, **report)
