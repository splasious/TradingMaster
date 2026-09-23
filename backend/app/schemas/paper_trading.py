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


class NativeHoldingOut(NativeLegOut):
    """One entry of deployment.state["holdings"] (see
    NativeDeploymentOut.holdings): the leg itself, when it was opened, and
    `metrics` -- every other scalar the strategy recorded on that holding
    (e.g. a rotation strategy's rank/rsi/macd, or nifty_rs_rotation's
    rs_value), passed through as-is so the dashboard can show why each
    stock is held without this schema knowing any one strategy's fields."""

    opened_at: datetime | None = None
    metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)


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
    # Every other scalar the strategy stored on the position (pcr_at_entry,
    # entry_spot, expiry, ...), passed through as-is -- same idea as
    # NativeHoldingOut.metrics -- plus the legs' underlying and its live
    # price, so the dashboard can show e.g. entry spot vs spot now.
    metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    underlying_symbol: str | None = None
    underlying_price: float | None = None


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
    # Display-only summary of deployment.state["holdings"], the OTHER real
    # state shape that exists today (see native_strategies' MACD/RSI
    # rotation strategy): a dict of independently-opened long equity
    # positions, each with its own entry time -- fundamentally not "one
    # position" (NativePositionOut's single bias/opened_at/legs-belong-
    # together shape doesn't fit multiple unrelated holdings), so this is
    # a flat list of legs instead (NativeHoldingOut: a NativeLegOut plus
    # its own opened_at and strategy metrics; side is always "long" here,
    # strike/option_type stay None -- equities have neither).
    holdings: list[NativeHoldingOut] | None = None
    created_at: datetime
    stopped_at: datetime | None


class NativeTradeOut(BaseModel):
    """One closed trade as a readable record (see paper_trading/
    trade_record.py): `legs` carry their contract details and own P&L;
    entry_price/exit_price/quantity/lots summarize the whole trade -- the
    per-unit net premium for a multi-leg one -- and are None when the legs
    don't share one quantity. `pnl` is gross; net_pnl = pnl - charges,
    with charges an estimate (None where not estimable)."""

    id: str
    deployment_id: str
    strategy_name: str | None = None
    currency: str | None = None
    opened_at: datetime
    closed_at: datetime
    legs: list[dict]
    underlying_symbol: str | None = None
    structure: str | None = None
    side: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    quantity: float | None = None
    lots: float | None = None
    pnl: float
    charges: float | None = None
    net_pnl: float
    pnl_pct: float
    exit_reason: str


class NativeEvaluationOut(BaseModel):
    action: str
    signal: str | None
    reason: str | None
