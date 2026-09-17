import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.user import User
from app.schemas.options import ChainRowOut, EffectivePcrOut, ExpiryOut, HistoryDepthOut, PcrPointOut, UnderlyingOut
from app.services.market_data.tick_engine import tick_engine
from app.services.options.chain import get_option_chain_snapshot
from app.services.options.history_depth import get_history_depth
from app.services.options.pcr import compute_effective_pcr, compute_pcr_series

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


@router.get("/{underlying_instrument_id}/chain", response_model=list[ChainRowOut])
async def get_chain(
    underlying_instrument_id: str, expiry: date = Query(...),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> list[ChainRowOut]:
    """Snapshot seed for the option chain table -- the frontend layers live
    WS ticks on top of this for real-time LTP/OI (see options/chain.py)."""
    rows = await get_option_chain_snapshot(db, uuid.UUID(underlying_instrument_id), expiry)
    return [ChainRowOut(**row) for row in rows]


@router.get("/{underlying_instrument_id}/history-depth", response_model=HistoryDepthOut)
async def get_history_depth_endpoint(
    underlying_instrument_id: str, expiry: date = Query(...),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> HistoryDepthOut:
    """How much historical data actually exists for this expiry -- both
    what this app has backfilled (our_*) and what Kite's own API reports
    live right now (kite_*), queried through the connected Zerodha session
    (see options/history_depth.py)."""
    result = await get_history_depth(db, uuid.UUID(underlying_instrument_id), expiry)
    return HistoryDepthOut(**result)


@router.get("/effective-pcr", response_model=EffectivePcrOut)
async def get_effective_pcr(
    underlying_symbol: str = Query("NIFTY 50"), num_expiries: int = Query(4), timeframe: str = Query("15m"),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> EffectivePcrOut:
    """The live PCR ticker for the Paper Trading page's Advanced Strategy
    Deployments panel -- same number and same defaults a PCR-driven native
    strategy (see native_strategies/nifty_pcr_credit_spread.py) actually
    reads each tick via ctx.get_pcr(), not the per-expiry chart series
    below."""
    pcr = await compute_effective_pcr(db, underlying_symbol=underlying_symbol, num_expiries=num_expiries, timeframe=timeframe)
    if pcr is None:
        bias = "unavailable"
    elif pcr < 1:
        bias = "bearish"
    elif pcr > 1:
        bias = "bullish"
    else:
        bias = "neutral"

    underlying = (await db.execute(select(Instrument).where(Instrument.symbol == underlying_symbol))).scalar_one_or_none()
    spot_price = tick_engine.get_current_price(underlying.id) if underlying is not None else None

    return EffectivePcrOut(
        underlying_symbol=underlying_symbol, num_expiries=num_expiries, timeframe=timeframe, pcr=pcr, bias=bias,
        spot_price=spot_price,
    )


@router.get("/{underlying_instrument_id}/pcr", response_model=list[PcrPointOut])
async def get_pcr(
    underlying_instrument_id: str, expiry: date = Query(...), timeframe: str = Query("15m"),
    db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user),
) -> list[PcrPointOut]:
    series = await compute_pcr_series(db, uuid.UUID(underlying_instrument_id), expiry, timeframe)
    return [PcrPointOut(**point) for point in series]
