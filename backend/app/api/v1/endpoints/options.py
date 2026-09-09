import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.user import User
from app.schemas.options import ExpiryOut, PcrPointOut, UnderlyingOut
from app.services.options.pcr import compute_pcr_series

router = APIRouter()


@router.get("/underlyings", response_model=list[UnderlyingOut])
async def list_underlyings(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[UnderlyingOut]:
    """Every instrument actually referenced as an F&O underlying -- derived
    from real backfilled NFO contracts, not a hardcoded index list."""
    referenced = select(Instrument.underlying_instrument_id).where(Instrument.underlying_instrument_id.is_not(None)).distinct()
    result = await db.execute(select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(referenced)).order_by(Instrument.symbol))
    return [UnderlyingOut(instrument_id=str(i), symbol=s) for i, s in result.all()]


@router.get("/{underlying_instrument_id}/expiries", response_model=list[ExpiryOut])
async def list_expiries(
    underlying_instrument_id: str, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)
) -> list[ExpiryOut]:
    result = await db.execute(
        select(Instrument.expiry, Instrument.instrument_type, func.count())
        .where(Instrument.underlying_instrument_id == uuid.UUID(underlying_instrument_id), Instrument.expiry.is_not(None))
        .group_by(Instrument.expiry, Instrument.instrument_type)
    )
    by_expiry: dict[date, dict[str, int]] = {}
    for expiry, itype, count in result.all():
        by_expiry.setdefault(expiry, {"future": 0, "option": 0})[itype] = count
    return [
        ExpiryOut(expiry=exp, future_count=counts["future"], option_count=counts["option"])
        for exp, counts in sorted(by_expiry.items())
    ]


@router.get("/{underlying_instrument_id}/pcr", response_model=list[PcrPointOut])
async def get_pcr(
    underlying_instrument_id: str, expiry: date = Query(...), timeframe: str = Query("15m"),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> list[PcrPointOut]:
    series = await compute_pcr_series(db, uuid.UUID(underlying_instrument_id), expiry, timeframe)
    return [PcrPointOut(**point) for point in series]
