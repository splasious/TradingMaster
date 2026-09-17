from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DeploymentCreate(BaseModel):
    strategy_id: str
    instrument_id: str
    portfolio_id: str
    timeframe: str = "1d"


class DeploymentOut(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    instrument_id: str
    instrument_symbol: str
    portfolio_id: str
    portfolio_name: str
    currency: str
    timeframe: str
    status: str
    last_evaluated_at: datetime | None
    last_signal: str | None = None
    last_signal_reason: str | None = None
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


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    currency: Literal["USD", "INR"] = "INR"
    initial_capital: float = Field(gt=0, default=100000.0)


class PortfolioUpdate(BaseModel):
    name: str | None = None
    initial_capital: float = Field(gt=0)


class PortfolioOut(BaseModel):
    id: str
    name: str
    currency: str
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
    id: str
    deployment_id: str
    # Populated only for the portfolio-wide listing (no deployment_id
    # filter) -- the per-deployment listing already has this context from
    # the page it's rendered on, so it leaves these null rather than
    # re-fetching what the caller already knows.
    instrument_symbol: str | None = None
    strategy_name: str | None = None
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str


class EvaluationOut(BaseModel):
    action: str
    signal: str | None
    price: float | None
    reason: str | None


class NativeDeploymentCreate(BaseModel):
    strategy_id: str
    portfolio_id: str
    # No instrument_id, no timeframe -- an "Advanced Python" strategy
    # picks its own instrument(s) live (see native_runner.py).


class NativeLegOut(BaseModel):
    instrument_symbol: str
    strike: float | None
    option_type: str | None
    side: str  # "short" | "long"
    quantity: float
    entry_price: float
    current_price: float | None


class NativePositionOut(BaseModel):
    """Display-only summary of deployment.state["position"] for the
    short/long-leg credit-spread convention native_strategies/
    nifty_pcr_credit_spread.py uses -- not every native strategy's state
    will look like this, but it's the one real shape that exists today
    (see native_runner.py's docstring: state is otherwise a strategy-owned
    blob, no fixed schema). trade_value is the net credit received at
    entry, live_value the net debit it would cost to close right now --
    unrealized_pnl = trade_value - live_value, the same formula the
    strategy's own close_position() uses."""

    bias: str | None
    opened_at: datetime
    legs: list[NativeLegOut]
    trade_value: float
    live_value: float | None
    unrealized_pnl: float | None


class NativeDeploymentOut(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    portfolio_id: str
    portfolio_name: str
    currency: str
    status: str
    last_evaluated_at: datetime | None
    last_signal: str | None = None
    last_signal_reason: str | None = None
    state: dict | None = None
    position: NativePositionOut | None = None
    created_at: datetime
    stopped_at: datetime | None


class NativeTradeOut(BaseModel):
    id: str
    deployment_id: str
    strategy_name: str | None = None
    opened_at: datetime
    closed_at: datetime
    legs: list[dict]
    pnl: float
    pnl_pct: float
    exit_reason: str


class NativeEvaluationOut(BaseModel):
    action: str
    signal: str | None
    reason: str | None
