from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator


class NativeBacktestJobCreate(BaseModel):
    strategy_id: str
    start_date: date
    end_date: date
    initial_capital: float = Field(default=100000.0, gt=0)

    @model_validator(mode="after")
    def _date_range_valid(self) -> "NativeBacktestJobCreate":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class NativeBacktestJobOut(BaseModel):
    id: str
    strategy_id: str
    start_date: date
    end_date: date
    initial_capital: float
    status: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class NativeBacktestResultOut(BaseModel):
    metrics: dict
    equity_curve: list[list]


class NativeBacktestTradeOut(BaseModel):
    id: str
    opened_at: datetime
    closed_at: datetime
    legs: list[dict]
    pnl: float
    pnl_pct: float
    exit_reason: str
