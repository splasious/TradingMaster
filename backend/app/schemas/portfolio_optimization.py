from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.optimization import ParamRangeIn


class PortfolioOptimizationJobCreate(BaseModel):
    strategy_id: str
    instrument_ids: list[str] = Field(min_length=2)  # a "portfolio" of one is just OptimizationJob -- use that instead
    timeframe: str = "1d"
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: float = Field(default=100000.0, gt=0)
    position_size_pct: float = Field(default=10.0, gt=0, le=100)
    max_open_positions: int = Field(default=10, gt=0)
    brokerage_pct: float = Field(default=0.03, ge=0)
    slippage_pct: float = Field(default=0.05, ge=0)
    tax_pct: float = Field(default=0.0, ge=0)
    param_ranges: list[ParamRangeIn]
    rank_metric: str = "sharpe_ratio"

    @field_validator("instrument_ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        seen = list(dict.fromkeys(value))
        if len(seen) < 2:
            raise ValueError("A portfolio optimization needs at least 2 distinct instruments")
        return seen

    @model_validator(mode="after")
    def _dates_in_order(self) -> "PortfolioOptimizationJobCreate":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class PortfolioOptimizationJobOut(BaseModel):
    id: str
    strategy_id: str
    instrument_ids: list[str]
    timeframe: str
    start_date: date | None
    end_date: date | None
    initial_capital: float
    position_size_pct: float
    max_open_positions: int
    param_ranges: list[ParamRangeIn]
    rank_metric: str
    status: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class PortfolioOptimizationRunOut(BaseModel):
    params: dict[str, float]
    metrics: dict
    instrument_count: int
    skipped_symbols: list[str]


class PortfolioOptimizationResultOut(BaseModel):
    runs: list[PortfolioOptimizationRunOut]
