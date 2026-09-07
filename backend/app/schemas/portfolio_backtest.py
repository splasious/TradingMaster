from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class PortfolioBacktestJobCreate(BaseModel):
    strategy_id: str
    instrument_ids: list[str] = Field(min_length=2)  # a "portfolio" of one is just BacktestJob -- use that instead
    timeframe: str = "1d"
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: float = Field(default=100000.0, gt=0)
    position_size_pct: float = Field(default=10.0, gt=0, le=100)
    max_open_positions: int = Field(default=10, gt=0)
    brokerage_pct: float = Field(default=0.03, ge=0)
    slippage_pct: float = Field(default=0.05, ge=0)
    tax_pct: float = Field(default=0.0, ge=0)

    @field_validator("instrument_ids")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        seen = list(dict.fromkeys(value))
        if len(seen) < 2:
            raise ValueError("A portfolio backtest needs at least 2 distinct instruments")
        return seen

    @model_validator(mode="after")
    def _dates_in_order(self) -> "PortfolioBacktestJobCreate":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class PortfolioBacktestJobOut(BaseModel):
    id: str
    strategy_id: str
    instrument_ids: list[str]
    timeframe: str
    start_date: date | None
    end_date: date | None
    initial_capital: float
    position_size_pct: float
    max_open_positions: int
    status: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class PortfolioBacktestResultOut(BaseModel):
    metrics: dict
    equity_curve: list[list]
    instrument_count: int
    skipped_symbols: list[str]


class PortfolioBacktestTradeOut(BaseModel):
    instrument_id: str
    symbol: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None
    exit_price: float | None
    quantity: float
    pnl: float
    pnl_pct: float
    bars_held: int
    exit_reason: str
    status: str
    side: str
