from datetime import datetime

from pydantic import BaseModel


class DeploymentCreate(BaseModel):
    strategy_id: str
    instrument_id: str
    timeframe: str = "1d"


class DeploymentOut(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    instrument_id: str
    instrument_symbol: str
    timeframe: str
    status: str
    last_evaluated_at: datetime | None
    created_at: datetime
    stopped_at: datetime | None
    open_position: "PositionOut | None" = None


class PositionOut(BaseModel):
    instrument_symbol: str
    quantity: float
    avg_entry_price: float
    current_price: float | None
    unrealized_pnl: float | None
    opened_at: datetime


class PortfolioOut(BaseModel):
    cash: float
    initial_capital: float
    equity: float
    unrealized_pnl: float
    realized_pnl_total: float
    positions: list[PositionOut]


class OrderOut(BaseModel):
    id: str
    side: str
    quantity: float
    price: float
    status: str
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TradeOut(BaseModel):
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float

    model_config = {"from_attributes": True}


class EvaluationOut(BaseModel):
    action: str
    signal: str | None
    price: float | None
    reason: str | None
