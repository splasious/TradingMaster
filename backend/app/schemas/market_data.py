from datetime import datetime

from pydantic import BaseModel


class BackfillRequest(BaseModel):
    instrument_id: str
    timeframe: str


class BackfillJobOut(BaseModel):
    id: str
    instrument_id: str
    timeframe: str
    status: str
    downloaded_count: int
    inserted_count: int
    duplicate_count: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class CandleOut(BaseModel):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None

    model_config = {"from_attributes": True}


class QualityReportOut(BaseModel):
    instrument_id: str
    timeframe: str
    candle_count: int
    invalid_ohlc_count: int
    non_positive_price_count: int
    missing_weekday_gaps: int
    quality_score: float
