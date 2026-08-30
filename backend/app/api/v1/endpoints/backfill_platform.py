import csv
import io
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.backfill_platform import (
    BfBackfillJob,
    BfBackfillStatus,
    BfOhlcvBar,
    BfSymbol,
    BfWatchlist,
    BfWatchlistItem,
)
from app.models.user import User
from app.schemas.backfill_platform import (
    BfBackfillJobCreate,
    BfBackfillJobOut,
    BfWatchlistCreate,
    BfWatchlistItemOut,
    BfWatchlistOut,
    BulkBackfillResult,
    CompletenessOut,
    CompletenessSegmentOut,
    LiveSyncStatusOut,
    ResampledCandleOut,
    SourceStatusOut,
    SymbolSearchResultOut,
    TimeframeOptionOut,
    CatalogSyncItemOut,
    WatchlistBulkAddResult,
    WatchlistCatalogSyncResult,
    WatchlistImportResult,
    WatchlistItemAdd,
    WatchlistItemBulkAdd,
)
from app.services.audit import write_audit_log
from app.services.backfill_platform import export as export_service
from app.services.backfill_platform import status as status_service
from app.services.backfill_platform import symbols as symbols_service
from app.services.backfill_platform.catalog_sync import CatalogSyncError, sync_symbol_to_catalog
from app.services.backfill_platform.completeness import compute_completeness
from app.services.backfill_platform.jobs import run_bf_backfill_job
from app.services.backfill_platform.live_sync_scheduler import bf_live_sync_scheduler
from app.services.backfill_platform.timeframes import timeframes_for_source
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.resample import resample_candles

router = APIRouter()

_VALID_SOURCES = ("yahoo", "delta", "zerodha")


def _check_source(source: str) -> None:
    if source not in _VALID_SOURCES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"source must be one of {_VALID_SOURCES}")


# ---------------------------------------------------------------- status --


@router.get("/sources/{source}/status", response_model=SourceStatusOut)
async def get_source_status(
    source: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SourceStatusOut:
    _check_source(source)
    if source == "yahoo":
        result = await status_service.yahoo_status()
    elif source == "delta":
        result = await status_service.delta_status()
    else:
        result = await status_service.zerodha_status(db, user.id)
    return SourceStatusOut(source=result.source, connected=result.connected, detail=result.detail, expires_at=result.expires_at)


@router.get("/live-sync/status", response_model=LiveSyncStatusOut)
async def get_live_sync_status(_: User = Depends(get_current_user)) -> LiveSyncStatusOut:
    return LiveSyncStatusOut(
        running=bf_live_sync_scheduler.running, last_sync_at=bf_live_sync_scheduler.last_sync_at,
        last_synced_count=bf_live_sync_scheduler.last_synced_count, last_error=bf_live_sync_scheduler.last_error,
    )


@router.get("/sources/{source}/timeframes", response_model=list[TimeframeOptionOut])
async def get_source_timeframes(source: str, _: User = Depends(get_current_user)) -> list[TimeframeOptionOut]:
    _check_source(source)
    return [TimeframeOptionOut(value=o.value, native=o.native) for o in timeframes_for_source(source)]


@router.post("/sources/{source}/backfill-all", response_model=BulkBackfillResult, status_code=status.HTTP_202_ACCEPTED)
async def backfill_all_for_source(
    source: str, background_tasks: BackgroundTasks, timeframe: str = Query("1d"),
    start_date: date | None = Query(None), end_date: date | None = Query(None),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator")),
) -> BulkBackfillResult:
    """Pulls the entire tracked universe for a source -- all ~750
    nse-yahoo-data NSE symbols, or all of Delta's RWA tokens -- queuing one
    background job per symbol rather than blocking the request on however
    long hundreds of real network calls take."""
    _check_source(source)
    try:
        all_symbols = await symbols_service.list_all_symbols(db, source, user.id)
    except MarketDataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    queued = 0
    for result in all_symbols:
        symbol = await symbols_service.get_or_create_symbol(db, source, result.symbol, result.display_name)
        job = BfBackfillJob(
            symbol_id=symbol.id, source=source, timeframe=timeframe,
            start_date=start_date, end_date=end_date, requested_by=user.id,
        )
        db.add(job)
        await db.flush()
        background_tasks.add_task(run_bf_backfill_job, job.id)
        queued += 1

    await write_audit_log(
        db, user_id=user.id, action="BF_BULK_BACKFILL_STARTED", object_type="bf_source", object_id=source,
        new_value={"source": source, "queued": queued, "timeframe": timeframe},
    )
    await db.commit()
    return BulkBackfillResult(source=source, queued=queued)


@router.get("/candles/resampled", response_model=list[ResampledCandleOut])
async def get_resampled_candles(
    source: str = Query(...), symbol: str = Query(...), target_timeframe: str = Query(...),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> list[ResampledCandleOut]:
    """Weekly/monthly views derived from stored daily bars (PRD's own
    per-block timeframe selector, extended here to cover timeframes a
    source doesn't natively provide) -- reuses the same resample engine
    the main platform's multi-timeframe charts use, so "still-forming
    period" look-ahead protection applies here too."""
    _check_source(source)
    symbol_row = (
        await db.execute(select(BfSymbol).where(BfSymbol.source == source, BfSymbol.symbol == symbol))
    ).scalar_one_or_none()
    if symbol_row is None:
        return []
    daily_bars = (
        await db.execute(
            select(BfOhlcvBar).where(BfOhlcvBar.symbol_id == symbol_row.id, BfOhlcvBar.timeframe == "1d").order_by(BfOhlcvBar.ts)
        )
    ).scalars().all()
    bars = [{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume} for b in daily_bars]
    try:
        resampled = resample_candles(bars, target_timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return [ResampledCandleOut(**bar) for bar in resampled]


# --------------------------------------------------------------- symbols --


@router.get("/sources/{source}/symbols", response_model=list[SymbolSearchResultOut])
async def search_source_symbols(
    source: str, q: str = Query(...), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[SymbolSearchResultOut]:
    _check_source(source)
    try:
        results = await symbols_service.search_symbols(db, source, q, user.id)
    except MarketDataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return [SymbolSearchResultOut(symbol=r.symbol, display_name=r.display_name) for r in results]


# ------------------------------------------------------------------ jobs --


def _job_out(job: BfBackfillJob, symbol: BfSymbol) -> BfBackfillJobOut:
    return BfBackfillJobOut(
        id=str(job.id), symbol_id=str(job.symbol_id), symbol=symbol.symbol, display_name=symbol.display_name,
        source=job.source, timeframe=job.timeframe, start_date=job.start_date, end_date=job.end_date,
        status=job.status, downloaded_count=job.downloaded_count, inserted_count=job.inserted_count,
        duplicate_count=job.duplicate_count, error_message=job.error_message, created_at=job.created_at,
        started_at=job.started_at, completed_at=job.completed_at,
    )


@router.post("/jobs", response_model=BfBackfillJobOut, status_code=status.HTTP_202_ACCEPTED)
async def create_backfill_job(
    payload: BfBackfillJobCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> BfBackfillJobOut:
    _check_source(payload.source)
    symbol = await symbols_service.get_or_create_symbol(db, payload.source, payload.symbol, payload.display_name)

    job = BfBackfillJob(
        symbol_id=symbol.id, source=payload.source, timeframe=payload.timeframe,
        start_date=payload.start_date, end_date=payload.end_date, requested_by=user.id,
    )
    db.add(job)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="BF_BACKFILL_STARTED", object_type="bf_symbol", object_id=str(symbol.id),
        new_value={"source": payload.source, "symbol": payload.symbol, "timeframe": payload.timeframe},
    )
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_bf_backfill_job, job.id)
    return _job_out(job, symbol)


@router.get("/jobs/{job_id}", response_model=BfBackfillJobOut)
async def get_backfill_job(
    job_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> BfBackfillJobOut:
    job = await db.get(BfBackfillJob, uuid.UUID(job_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    symbol = await db.get(BfSymbol, job.symbol_id)
    return _job_out(job, symbol)


@router.get("/jobs", response_model=list[BfBackfillJobOut])
async def list_backfill_jobs(
    source: str | None = Query(None), db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> list[BfBackfillJobOut]:
    stmt = select(BfBackfillJob).order_by(BfBackfillJob.created_at.desc()).limit(50)
    if source:
        _check_source(source)
        stmt = stmt.where(BfBackfillJob.source == source)
    jobs = (await db.execute(stmt)).scalars().all()
    out = []
    for job in jobs:
        symbol = await db.get(BfSymbol, job.symbol_id)
        if symbol is not None:
            out.append(_job_out(job, symbol))
    return out


@router.post("/jobs/{job_id}/retry", response_model=BfBackfillJobOut, status_code=status.HTTP_202_ACCEPTED)
async def retry_backfill_job(
    job_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> BfBackfillJobOut:
    original = await db.get(BfBackfillJob, uuid.UUID(job_id))
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    symbol = await db.get(BfSymbol, original.symbol_id)

    job = BfBackfillJob(
        symbol_id=original.symbol_id, source=original.source, timeframe=original.timeframe,
        start_date=original.start_date, end_date=original.end_date, requested_by=user.id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    background_tasks.add_task(run_bf_backfill_job, job.id)
    return _job_out(job, symbol)


# ------------------------------------------------------------ completeness --


@router.get("/completeness", response_model=CompletenessOut)
async def get_completeness(
    source: str = Query(...), symbol: str = Query(...), timeframe: str = Query(...),
    start: date = Query(...), end: date = Query(...),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> CompletenessOut:
    _check_source(source)
    symbol_row = (
        await db.execute(select(BfSymbol).where(BfSymbol.source == source, BfSymbol.symbol == symbol))
    ).scalar_one_or_none()
    bar_dates: set[date] = set()
    if symbol_row is not None:
        rows = (
            await db.execute(
                select(BfOhlcvBar.ts).where(BfOhlcvBar.symbol_id == symbol_row.id, BfOhlcvBar.timeframe == timeframe)
            )
        ).scalars().all()
        bar_dates = {ts.date() for ts in rows}
    segments = compute_completeness(bar_dates, start, end, source)
    return CompletenessOut(segments=[CompletenessSegmentOut(start=s.start, end=s.end, status=s.status) for s in segments])


# ------------------------------------------------------------- watchlists --


async def _watchlist_summary(db: AsyncSession, wl: BfWatchlist) -> BfWatchlistOut:
    items = (await db.execute(select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == wl.id))).scalars().all()
    never_backfilled = 0
    last_backfill_at: datetime | None = None
    for item in items:
        bar_count = (
            await db.execute(select(func.count()).select_from(BfOhlcvBar).where(BfOhlcvBar.symbol_id == item.symbol_id))
        ).scalar_one()
        if bar_count == 0:
            never_backfilled += 1
        latest_job = (
            await db.execute(
                select(BfBackfillJob)
                .where(BfBackfillJob.symbol_id == item.symbol_id, BfBackfillJob.status == BfBackfillStatus.COMPLETED.value)
                .order_by(BfBackfillJob.completed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_job is not None and latest_job.completed_at is not None:
            if last_backfill_at is None or latest_job.completed_at > last_backfill_at:
                last_backfill_at = latest_job.completed_at

    return BfWatchlistOut(
        id=str(wl.id), name=wl.name, tags=wl.tags, symbol_count=len(items), never_backfilled_count=never_backfilled,
        last_backfill_at=last_backfill_at, created_at=wl.created_at, updated_at=wl.updated_at,
    )


async def _load_owned_watchlist(db: AsyncSession, watchlist_id: str, user: User) -> BfWatchlist:
    wl = await db.get(BfWatchlist, uuid.UUID(watchlist_id))
    if wl is None or (wl.owner_id != user.id and "administrator" not in user.role_names):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist not found")
    return wl


@router.post("/watchlists", response_model=BfWatchlistOut, status_code=status.HTTP_201_CREATED)
async def create_watchlist(
    payload: BfWatchlistCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> BfWatchlistOut:
    wl = BfWatchlist(owner_id=user.id, name=payload.name, tags=payload.tags)
    db.add(wl)
    await db.commit()
    await db.refresh(wl)
    return await _watchlist_summary(db, wl)


@router.get("/watchlists", response_model=list[BfWatchlistOut])
async def list_watchlists(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[BfWatchlistOut]:
    stmt = select(BfWatchlist).order_by(BfWatchlist.created_at)
    if "administrator" not in user.role_names:
        stmt = stmt.where(BfWatchlist.owner_id == user.id)
    watchlists = (await db.execute(stmt)).scalars().all()
    return [await _watchlist_summary(db, wl) for wl in watchlists]


@router.patch("/watchlists/{watchlist_id}", response_model=BfWatchlistOut)
async def rename_watchlist(
    watchlist_id: str, payload: BfWatchlistCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> BfWatchlistOut:
    wl = await _load_owned_watchlist(db, watchlist_id, user)
    wl.name = payload.name
    wl.tags = payload.tags
    await db.commit()
    await db.refresh(wl)
    return await _watchlist_summary(db, wl)


@router.delete("/watchlists/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist(watchlist_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    wl = await _load_owned_watchlist(db, watchlist_id, user)
    await db.delete(wl)
    await db.commit()


@router.get("/watchlists/{watchlist_id}/items", response_model=list[BfWatchlistItemOut])
async def list_watchlist_items(
    watchlist_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[BfWatchlistItemOut]:
    await _load_owned_watchlist(db, watchlist_id, user)
    items = (
        await db.execute(select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == uuid.UUID(watchlist_id)))
    ).scalars().all()
    out = []
    for item in items:
        symbol = await db.get(BfSymbol, item.symbol_id)
        if symbol is None:
            continue
        bar_count = (
            await db.execute(select(func.count()).select_from(BfOhlcvBar).where(BfOhlcvBar.symbol_id == symbol.id))
        ).scalar_one()
        latest_job = (
            await db.execute(
                select(BfBackfillJob).where(BfBackfillJob.symbol_id == symbol.id).order_by(BfBackfillJob.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        out.append(BfWatchlistItemOut(
            id=str(item.id), symbol_id=str(symbol.id), source=symbol.source, symbol=symbol.symbol,
            display_name=symbol.display_name, bar_count=bar_count, last_job_status=latest_job.status if latest_job else None,
        ))
    return out


@router.post("/watchlists/{watchlist_id}/items", response_model=BfWatchlistItemOut, status_code=status.HTTP_201_CREATED)
async def add_watchlist_item(
    watchlist_id: str, payload: WatchlistItemAdd, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> BfWatchlistItemOut:
    _check_source(payload.source)
    wl = await _load_owned_watchlist(db, watchlist_id, user)
    symbol = await symbols_service.get_or_create_symbol(db, payload.source, payload.symbol, payload.display_name)

    existing = (
        await db.execute(
            select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == wl.id, BfWatchlistItem.symbol_id == symbol.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Symbol already in this watchlist")

    item = BfWatchlistItem(watchlist_id=wl.id, symbol_id=symbol.id)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return BfWatchlistItemOut(
        id=str(item.id), symbol_id=str(symbol.id), source=symbol.source, symbol=symbol.symbol,
        display_name=symbol.display_name, bar_count=0, last_job_status=None,
    )


@router.post("/watchlists/{watchlist_id}/items/bulk", response_model=WatchlistBulkAddResult)
async def bulk_add_watchlist_items(
    watchlist_id: str, payload: WatchlistItemBulkAdd, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> WatchlistBulkAddResult:
    wl = await _load_owned_watchlist(db, watchlist_id, user)

    existing_symbol_ids = {
        row[0]
        for row in (
            await db.execute(select(BfWatchlistItem.symbol_id).where(BfWatchlistItem.watchlist_id == wl.id))
        ).all()
    }

    added = 0
    skipped = 0
    for entry in payload.items:
        _check_source(entry.source)
        symbol = await symbols_service.get_or_create_symbol(db, entry.source, entry.symbol, entry.display_name)
        if symbol.id in existing_symbol_ids:
            skipped += 1
            continue
        db.add(BfWatchlistItem(watchlist_id=wl.id, symbol_id=symbol.id))
        existing_symbol_ids.add(symbol.id)
        added += 1

    await db.commit()
    return WatchlistBulkAddResult(added=added, skipped=skipped)


@router.post("/watchlists/{watchlist_id}/sync-to-catalog", response_model=WatchlistCatalogSyncResult)
async def sync_watchlist_to_catalog(
    watchlist_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> WatchlistCatalogSyncResult:
    """Copies this watchlist's real backfilled bars from the isolated bf_*
    schema into the main Instrument/OhlcvCandle schema, so the symbols
    become usable in Charts, Strategy Builder, Backtesting, and
    Optimization -- an explicit, user-triggered action, never automatic."""
    wl = await _load_owned_watchlist(db, watchlist_id, user)
    items = (await db.execute(select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == wl.id))).scalars().all()

    results: list[CatalogSyncItemOut] = []
    for item in items:
        symbol = await db.get(BfSymbol, item.symbol_id)
        if symbol is None:
            continue
        try:
            sync_result = await sync_symbol_to_catalog(db, symbol)
        except CatalogSyncError as exc:
            results.append(CatalogSyncItemOut(
                symbol=symbol.symbol, instrument_id=None, instrument_created=False,
                bars_synced=0, bars_skipped=0, error=str(exc),
            ))
            continue
        results.append(CatalogSyncItemOut(
            symbol=sync_result.symbol, instrument_id=sync_result.instrument_id,
            instrument_created=sync_result.instrument_created,
            bars_synced=sync_result.bars_synced, bars_skipped=sync_result.bars_skipped,
        ))

    await write_audit_log(
        db, user_id=user.id, action="BF_WATCHLIST_SYNCED_TO_CATALOG", object_type="bf_watchlist", object_id=str(wl.id),
        new_value={"synced_bars": sum(r.bars_synced for r in results), "instruments_created": sum(1 for r in results if r.instrument_created)},
    )
    await db.commit()
    return WatchlistCatalogSyncResult(items=results)


@router.delete("/watchlists/{watchlist_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_watchlist_item(
    watchlist_id: str, item_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    await _load_owned_watchlist(db, watchlist_id, user)
    item = await db.get(BfWatchlistItem, uuid.UUID(item_id))
    if item is None or str(item.watchlist_id) != watchlist_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    await db.delete(item)
    await db.commit()


@router.post("/watchlists/{watchlist_id}/backfill", response_model=list[BfBackfillJobOut], status_code=status.HTTP_202_ACCEPTED)
async def backfill_watchlist(
    watchlist_id: str, background_tasks: BackgroundTasks, timeframe: str = Query("1d"),
    start_date: date | None = Query(None), end_date: date | None = Query(None),
    db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> list[BfBackfillJobOut]:
    wl = await _load_owned_watchlist(db, watchlist_id, user)
    items = (await db.execute(select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == wl.id))).scalars().all()

    jobs_out = []
    for item in items:
        symbol = await db.get(BfSymbol, item.symbol_id)
        if symbol is None:
            continue
        job = BfBackfillJob(
            symbol_id=symbol.id, source=symbol.source, timeframe=timeframe,
            start_date=start_date, end_date=end_date, requested_by=user.id,
        )
        db.add(job)
        await db.flush()
        background_tasks.add_task(run_bf_backfill_job, job.id)
        jobs_out.append((job, symbol))

    await write_audit_log(
        db, user_id=user.id, action="BF_WATCHLIST_BACKFILL_STARTED", object_type="bf_watchlist", object_id=str(wl.id),
        new_value={"symbol_count": len(jobs_out), "timeframe": timeframe},
    )
    await db.commit()
    for job, _symbol in jobs_out:
        await db.refresh(job)
    return [_job_out(job, symbol) for job, symbol in jobs_out]


@router.get("/watchlists/{watchlist_id}/export.csv")
async def export_watchlist_csv(
    watchlist_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    await _load_owned_watchlist(db, watchlist_id, user)
    items = (
        await db.execute(select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == uuid.UUID(watchlist_id)))
    ).scalars().all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["source", "symbol", "display_name"])
    for item in items:
        symbol = await db.get(BfSymbol, item.symbol_id)
        if symbol is not None:
            writer.writerow([symbol.source, symbol.symbol, symbol.display_name])
    return Response(content=buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=watchlist.csv"})


@router.post("/watchlists/{watchlist_id}/import", response_model=WatchlistImportResult)
async def import_watchlist_csv(
    watchlist_id: str, file: UploadFile, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> WatchlistImportResult:
    wl = await _load_owned_watchlist(db, watchlist_id, user)
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))

    added = 0
    skipped = 0
    for row in reader:
        source = (row.get("source") or "").strip().lower()
        symbol_code = (row.get("symbol") or "").strip()
        display_name = (row.get("display_name") or symbol_code).strip()
        if source not in _VALID_SOURCES or not symbol_code:
            skipped += 1
            continue
        symbol = await symbols_service.get_or_create_symbol(db, source, symbol_code, display_name)
        existing = (
            await db.execute(
                select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == wl.id, BfWatchlistItem.symbol_id == symbol.id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            skipped += 1
            continue
        db.add(BfWatchlistItem(watchlist_id=wl.id, symbol_id=symbol.id))
        added += 1

    await db.commit()
    return WatchlistImportResult(added=added, skipped=skipped)


# ------------------------------------------------------------------ excel --


@router.get("/export/symbol.xlsx")
async def export_symbol_xlsx(
    source: str = Query(...), symbol: str = Query(...), timeframe: str = Query("1d"),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> Response:
    _check_source(source)
    symbol_row = (
        await db.execute(select(BfSymbol).where(BfSymbol.source == source, BfSymbol.symbol == symbol))
    ).scalar_one_or_none()
    if symbol_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Symbol not tracked yet -- backfill it first")
    content = await export_service.export_symbol(db, symbol_row, timeframe)
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     headers={"Content-Disposition": f"attachment; filename={source}_{symbol}.xlsx"})


@router.get("/watchlists/{watchlist_id}/export.xlsx")
async def export_watchlist_xlsx(
    watchlist_id: str, timeframe: str | None = Query(None), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    wl = await _load_owned_watchlist(db, watchlist_id, user)
    content = await export_service.export_watchlist(db, wl.id, timeframe)
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     headers={"Content-Disposition": "attachment; filename=watchlist.xlsx"})


@router.get("/export/all.xlsx")
async def export_all_xlsx(db: AsyncSession = Depends(get_db), _: User = Depends(require_role("administrator"))) -> Response:
    content = await export_service.export_all(db)
    return Response(content=content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     headers={"Content-Disposition": "attachment; filename=all_sources.xlsx"})
