from datetime import datetime

from pydantic import BaseModel, Field


class BacktestJobCreate(BaseModel):
    strategy_id: str
    instrument_id: str
    timeframe: str = "1d"
    initial_capital: float = Field(default=100000.0, gt=0)
    brokerage_pct: float = Field(default=0.03, ge=0)
    slippage_pct: float = Field(default=0.05, ge=0)
    tax_pct: float = Field(default=0.0, ge=0)
    out_of_sample_split_pct: float | None = Field(default=None, gt=0, lt=100)
    run_monte_carlo: bool = False


class BacktestJobOut(BaseModel):
    id: str
    strategy_id: str
    instrument_id: str
    timeframe: str
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
