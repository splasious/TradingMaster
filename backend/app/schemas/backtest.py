from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class BacktestJobCreate(BaseModel):
    strategy_id: str
    instrument_id: str
    timeframe: str = "1d"
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: float = Field(default=100000.0, gt=0)
    # Optional per-run override of the strategy version's own position
    # sizing; either both are set or neither is (partial overrides would be
    # ambiguous -- "value" alone doesn't mean anything without a type).
    position_sizing_type: Literal["fixed_quantity", "percent_capital"] | None = None
    position_sizing_value: float | None = Field(default=None, gt=0)
    brokerage_pct: float = Field(default=0.03, ge=0)
    slippage_pct: float = Field(default=0.05, ge=0)
    tax_pct: float = Field(default=0.0, ge=0)
    out_of_sample_split_pct: float | None = Field(default=None, gt=0, lt=100)
    run_monte_carlo: bool = False

    @model_validator(mode="after")
    def _sizing_type_and_value_together(self) -> "BacktestJobCreate":
        if (self.position_sizing_type is None) != (self.position_sizing_value is None):
            raise ValueError("position_sizing_type and position_sizing_value must be set together")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class BacktestJobOut(BaseModel):
    id: str
    strategy_id: str
    instrument_id: str
    timeframe: str
    start_date: date | None
    end_date: date | None
    initial_capital: float
    status: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class BacktestResultOut(BaseModel):
    metrics: dict
    out_of_sample_metrics: dict | None
    monte_carlo: dict | None
    equity_curve: list[list]


class BacktestTradeOut(BaseModel):
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str

    model_config = {"from_attributes": True}
