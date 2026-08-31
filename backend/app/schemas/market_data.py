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


class QuoteRequest(BaseModel):
    instrument_ids: list[str]


class QuoteOut(BaseModel):
    """The most recent stored daily bar's close/volume for an instrument --
    real data, not fabricated: no source in this system tracks a live,
    continuously-accumulating intraday volume, so "volume" here is the
    latest completed trading day's total, same as prev_close is used for
    day-over-day % change against the live tick price."""

    instrument_id: str
    prev_close: float
    volume: float | None
    ts: datetime


class QualityReportOut(BaseModel):
    instrument_id: str
    timeframe: str
    candle_count: int
    invalid_ohlc_count: int
    non_positive_price_count: int
    missing_weekday_gaps: int
    quality_score: float
