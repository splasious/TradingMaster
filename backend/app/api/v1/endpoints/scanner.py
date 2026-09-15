import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.scan import SavedScan
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.instrument import InstrumentOut
from app.schemas.scanner import (
    ScanMatch,
    ScanRequest,
    ScanResponse,
    SavedScanCreate,
    SavedScanOut,
    StrategyScanRequest,
    StrategyScanResponse,
    StrategySignalMatch,
)
from app.services.scanner import evaluate_condition, run_python_strategy_scan

router = APIRouter()


@router.post("/run", response_model=ScanResponse)
async def run_scan(
    payload: ScanRequest, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> ScanResponse:
    stmt = select(Instrument).where(Instrument.is_active.is_(True))
    if payload.exchange:
        stmt = stmt.where(Instrument.exchange == payload.exchange)
    instruments = (await db.execute(stmt)).scalars().all()

    matches: list[ScanMatch] = []
    for instrument in instruments:
        candles = (
            (
                await db.execute(
                    select(OhlcvCandle)
                    .where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == payload.timeframe)
                    .order_by(OhlcvCandle.ts)
                )
            )
            .scalars()
            .all()
        )
        if not candles:
            continue

        values: dict[str, float | None] = {}
        all_pass = True
        try:
            for condition in payload.conditions:
                passed, value = evaluate_condition(candles, condition)
                values[condition.field] = value
                if not passed:
                    all_pass = False
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        if all_pass:
            matches.append(
                ScanMatch(
                    instrument=InstrumentOut(
                        id=str(instrument.id), exchange=instrument.exchange, symbol=instrument.symbol,
                        name=instrument.name, instrument_type=instrument.instrument_type,
                        data_source=instrument.data_source, is_active=instrument.is_active,
                    ),
                    values=values,
                )
            )

    return ScanResponse(matched=matches, scanned_count=len(instruments))


@router.post("/run-strategy", response_model=StrategyScanResponse)
async def run_strategy_scan(
    payload: StrategyScanRequest, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> StrategyScanResponse:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id and "administrator" not in user.role_names:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")
    if strategy.code_type != "python":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy scanning is only supported for Python strategies")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    stmt = select(Instrument).where(Instrument.is_active.is_(True))
    if payload.exchange:
        stmt = stmt.where(Instrument.exchange == payload.exchange)
    instruments = (await db.execute(stmt)).scalars().all()

    signal_by_instrument, skipped_symbols = await run_python_strategy_scan(db, version, instruments)

    inst_by_id = {str(i.id): i for i in instruments}
    matched = [
        StrategySignalMatch(
            instrument=InstrumentOut(
                id=inst_id, exchange=inst_by_id[inst_id].exchange, symbol=inst_by_id[inst_id].symbol,
                name=inst_by_id[inst_id].name, instrument_type=inst_by_id[inst_id].instrument_type,
                data_source=inst_by_id[inst_id].data_source, is_active=inst_by_id[inst_id].is_active,
            ),
            signal=signal,
        )
        for inst_id, signal in signal_by_instrument.items()
        if signal != "HOLD"
    ]
    matched.sort(key=lambda m: m.instrument.symbol)

    return StrategyScanResponse(
        matched=matched, scanned_count=len(instruments), skipped_symbols=skipped_symbols,
        strategy_version_number=version.version_number, parameters=version.parameters,
    )


def _saved_out(scan: SavedScan) -> SavedScanOut:
    return SavedScanOut(
        id=str(scan.id), name=scan.name, exchange=scan.exchange, timeframe=scan.timeframe,
        conditions=scan.conditions,
    )


@router.get("/saved", response_model=list[SavedScanOut])
async def list_saved_scans(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[SavedScanOut]:
    result = await db.execute(select(SavedScan).where(SavedScan.user_id == user.id).order_by(SavedScan.created_at.desc()))
    return [_saved_out(s) for s in result.scalars().all()]


@router.post("/saved", response_model=SavedScanOut, status_code=status.HTTP_201_CREATED)
async def create_saved_scan(
    payload: SavedScanCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SavedScanOut:
    scan = SavedScan(
        user_id=user.id, name=payload.name, exchange=payload.exchange, timeframe=payload.timeframe,
        conditions=[c.model_dump() for c in payload.conditions],
    )
    db.add(scan)
    await db.commit()
    await db.refresh(scan)
    return _saved_out(scan)


@router.delete("/saved/{scan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_scan(
    scan_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    scan = await db.get(SavedScan, uuid.UUID(scan_id))
    if scan is None or scan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved scan not found")
    await db.delete(scan)
    await db.commit()
