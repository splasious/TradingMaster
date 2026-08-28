import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.market_data import OhlcvCandle
from app.models.user import User
from app.schemas.indicator import IndicatorPoint, IndicatorSpecOut
from app.services.indicators.base import candles_to_frame
from app.services.indicators.registry import INDICATOR_REGISTRY, get_indicator

router = APIRouter()


@router.get("", response_model=list[IndicatorSpecOut])
async def list_indicators(_: User = Depends(get_current_user)) -> list[IndicatorSpecOut]:
    return [
        IndicatorSpecOut(
            code=spec.code, name=spec.name, category=spec.category,
            output_fields=spec.output_fields, default_params=spec.default_params,
        )
        for spec in INDICATOR_REGISTRY.values()
    ]


@router.get("/calculate", response_model=list[IndicatorPoint])
async def calculate_indicator(
    instrument_id: str = Query(...),
    timeframe: str = Query(...),
    indicator: str = Query(...),
    params: str | None = Query(None, description="JSON object overriding default params"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[IndicatorPoint]:
    try:
        spec = get_indicator(indicator)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    param_overrides = {}
    if params:
        try:
            param_overrides = json.loads(params)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="params must be valid JSON") from exc

    result = await db.execute(
        select(OhlcvCandle)
        .where(OhlcvCandle.instrument_id == uuid.UUID(instrument_id), OhlcvCandle.timeframe == timeframe)
        .order_by(OhlcvCandle.ts)
    )
    candles = list(result.scalars().all())
    if not candles:
        return []

    df = candles_to_frame(candles)
    merged_params = {**spec.default_params, **param_overrides}
    computed = spec.compute(df, **merged_params)

    points: list[IndicatorPoint] = []
    for i in range(len(df)):
        values = {}
        for field in spec.output_fields:
            v = computed[field].iloc[i]
            values[field] = None if v != v else float(v)  # NaN check
        points.append(IndicatorPoint(ts=df["ts"].iloc[i], values=values))
    return points
