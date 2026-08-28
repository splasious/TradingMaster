from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.scanner import ScanCondition

RuleNode = dict  # {"all"/"any": [RuleNode, ...]} or a leaf ScanCondition-shaped dict


class PositionSizing(BaseModel):
    type: Literal["fixed_quantity", "percent_capital"] = "fixed_quantity"
    value: float = 1.0


class RiskRules(BaseModel):
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_positions: int | None = None
    max_daily_loss_pct: float | None = None


class StrategyVersionCreate(BaseModel):
    timeframe: str = "1d"
    instrument_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, float] = Field(default_factory=dict)
    entry_rules: RuleNode | None = None
    exit_rules: RuleNode | None = None
    python_code: str | None = Field(default=None, max_length=20000)
    position_sizing: PositionSizing = Field(default_factory=PositionSizing)
    risk_rules: RiskRules = Field(default_factory=RiskRules)

    @model_validator(mode="after")
    def require_matching_content(self):
        has_visual = self.entry_rules is not None
        has_python = bool(self.python_code and self.python_code.strip())
        if has_visual == has_python:
            raise ValueError("Provide exactly one of entry_rules (visual mode) or python_code (Python mode)")
        return self


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = None
    version: StrategyVersionCreate


class StrategyVersionOut(BaseModel):
    id: str
    version_number: int
    timeframe: str
    instrument_ids: list[str]
    parameters: dict[str, float]
    entry_rules: RuleNode | None
    exit_rules: RuleNode | None
    python_code: str | None
    position_sizing: PositionSizing
    risk_rules: RiskRules
    created_at: datetime


class StrategyOut(BaseModel):
    id: str
    name: str
    description: str | None
    code_type: str
    status: str
    owner_id: str
    created_at: datetime
    updated_at: datetime
    latest_version: StrategyVersionOut | None


class ValidateResult(BaseModel):
    valid: bool
    error: str | None = None
    sample_signal: str | None = None
