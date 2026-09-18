from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DeploymentCreate(BaseModel):
    strategy_id: str
    instrument_id: str
    broker_account_id: str
    timeframe: str = "1d"
    confirmed: bool = False  # explicit user confirmation, PRD section 25/49 -- not just a UI checkbox
    # Optional cap on how much of the broker's real available balance this
    # deployment may use for sizing -- never a locally-tracked wallet. None
    # sizes off the full broker balance, exactly as before this field existed.
    allocated_capital: float | None = Field(default=None, gt=0)


class SafetyCheckOut(BaseModel):
    passed: bool
    checks: dict[str, bool]
    failures: list[str]


class LivePositionOut(BaseModel):
    instrument_symbol: str
    quantity: float
    avg_entry_price: float
    opened_at: datetime


class LiveDeploymentOut(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    instrument_id: str
    instrument_symbol: str
    broker_account_id: str
    timeframe: str
    status: str
    allocated_capital: float | None
    currency: str | None
    last_evaluated_at: datetime | None
    last_signal: str | None = None
    last_signal_reason: str | None = None
    created_at: datetime
    stopped_at: datetime | None
    # Configured risk limits (stop_loss_pct/take_profit_pct/max_positions/
    # max_daily_loss_pct) and today's real realized P&L against them --
    # for the Risk Management page, at no extra round trip.
    risk_rules: dict = {}
    realized_pnl_today: float = 0.0
    open_position: LivePositionOut | None = None


class LiveOrderOut(BaseModel):
    id: str
    deployment_id: str | None
    strategy_name: str
    instrument_symbol: str
    client_order_id: str
    broker_order_id: str | None
    side: str
    quantity: float
    status: str
    reason: str | None
    created_at: datetime
    confirmed_at: datetime | None


class ManualOrderCreate(BaseModel):
    """A single real broker order fired directly (e.g. from a Market
    Scanner result row), with no strategy/deployment behind it -- see
    services/live_trading/manual_orders.py for the full safety pipeline
    this goes through before anything reaches the broker."""

    instrument_id: str
    broker_account_id: str
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(default=None, gt=0)
    product: Literal["CNC", "MIS", "NRML"]  # required -- never auto-inferred, unlike a deployment's product_override
    confirmed: bool = False  # explicit user confirmation, re-checked server-side -- same rule as DeploymentCreate.confirmed


class ManualOrderOut(BaseModel):
    id: str
    instrument_symbol: str
    broker_account_id: str
    client_order_id: str
    broker_order_id: str | None
    side: str
    quantity: float
    product: str | None
    status: str
    reason: str | None
    created_at: datetime
    confirmed_at: datetime | None


class LiveTradeOut(BaseModel):
    id: str
    deployment_id: str
    strategy_name: str
    instrument_symbol: str
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


class KillSwitchOut(BaseModel):
    active: bool
    activated_at: datetime | None
    reason: str | None


class KillSwitchActivate(BaseModel):
    reason: str


class ReconciliationOut(BaseModel):
    clean: bool
    matched: list[dict]
    local_only: list[dict]
    broker_only: list[dict]
    quantity_mismatches: list[dict]
