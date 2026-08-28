from datetime import datetime

from pydantic import BaseModel, Field


class ParamRangeIn(BaseModel):
    name: str
    min: float
    max: float
    step: float = Field(gt=0)


class OptimizationJobCreate(BaseModel):
    strategy_id: str
    instrument_id: str
    timeframe: str = "1d"
    initial_capital: float = Field(default=100000.0, gt=0)
    param_ranges: list[ParamRangeIn]
    rank_metric: str = "sharpe_ratio"


class OptimizationJobOut(BaseModel):
    id: str
    strategy_id: str
    instrument_id: str
    status: str
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None


class OptimizationRunOut(BaseModel):
    params: dict[str, float]
    metrics: dict


class OptimizationResultOut(BaseModel):
    runs: list[OptimizationRunOut]
