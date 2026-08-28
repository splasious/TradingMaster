from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.user import User
from app.schemas.instrument import InstrumentOut, InstrumentSyncResult
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.market_data.yahoo_source import YahooNSEDataSource

router = APIRouter()


def _out(instrument: Instrument) -> InstrumentOut:
    return InstrumentOut(
        id=str(instrument.id),
        exchange=instrument.exchange,
        symbol=instrument.symbol,
        name=instrument.name,
        instrument_type=instrument.instrument_type,
        data_source=instrument.data_source,
        is_active=instrument.is_active,
    )


@router.get("", response_model=list[InstrumentOut])
async def list_instruments(
    q: str | None = Query(None, description="Search by symbol or name"),
    exchange: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[InstrumentOut]:
    stmt = select(Instrument).where(Instrument.is_active.is_(True))
    if exchange:
        stmt = stmt.where(Instrument.exchange == exchange)
    if q:
        like = f"%{q.upper()}%"
        stmt = stmt.where(or_(Instrument.symbol.like(like), Instrument.name.ilike(f"%{q}%")))
    stmt = stmt.order_by(Instrument.exchange, Instrument.symbol).limit(200)
    result = await db.execute(stmt)
    return [_out(i) for i in result.scalars().all()]


@router.post("/sync/{data_source}", response_model=InstrumentSyncResult)
async def sync_instruments(
    data_source: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("administrator")),
) -> InstrumentSyncResult:
    existing_result = await db.execute(select(Instrument.exchange, Instrument.symbol))
    existing = {(row[0], row[1]) for row in existing_result.all()}

    created = 0
    if data_source == "yahoo_nse":
        try:
            symbols = await YahooNSEDataSource().list_symbols()
        except MarketDataSourceError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        for s in symbols:
            key = ("NSE", s["nse_code"])
            if key in existing or not s.get("is_active", True):
                continue
            db.add(
                Instrument(
                    exchange="NSE",
                    symbol=s["nse_code"],
                    name=s["name"] or s["nse_code"],
                    instrument_type="index" if " " in s["nse_code"] else "equity",
                    data_source="yahoo_nse",
                    external_ref=s["nse_code"],
                )
            )
            existing.add(key)
            created += 1
        found = len(symbols)
    elif data_source == "delta_exchange":
        try:
            products = await DeltaExchangeDataSource().list_products()
        except MarketDataSourceError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
        for p in products:
            key = ("DELTA", p["symbol"])
            if key in existing:
                continue
            db.add(
                Instrument(
                    exchange="DELTA",
                    symbol=p["symbol"],
                    name=p.get("description") or p["symbol"],
                    instrument_type="perpetual_future",
                    data_source="delta_exchange",
                    external_ref=p["symbol"],
                )
            )
            existing.add(key)
            created += 1
        found = len(products)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown data source '{data_source}'")

    await db.commit()
    return InstrumentSyncResult(data_source=data_source, found=found, created=created, skipped=found - created)
