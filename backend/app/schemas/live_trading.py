from datetime import datetime

from pydantic import BaseModel


class DeploymentCreate(BaseModel):
    strategy_id: str
    instrument_id: str
    broker_account_id: str
    timeframe: str = "1d"
    confirmed: bool = False  # explicit user confirmation, PRD section 25/49 -- not just a UI checkbox


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
    last_evaluated_at: datetime | None
    created_at: datetime
    stopped_at: datetime | None
    open_position: LivePositionOut | None = None


class LiveOrderOut(BaseModel):
    id: str
    client_order_id: str
    broker_order_id: str | None
    side: str
    quantity: float
    status: str
    reason: str | None
    created_at: datetime
    confirmed_at: datetime | None


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
