from typing import Any

from pydantic import BaseModel, Field


class BrokerOut(BaseModel):
    id: str
    code: str
    name: str
    is_enabled: bool
    is_real_adapter: bool

    model_config = {"from_attributes": True}


class BrokerAccountOut(BaseModel):
    id: str
    broker: BrokerOut
    account_label: str
    environment: str
    is_active: bool
    connection_status: str
    connection_last_error: str | None = None

    model_config = {"from_attributes": True}


class BrokerAccountCreate(BaseModel):
    broker_code: str
    account_label: str
    environment: str = "paper"
    credentials: dict[str, Any] = Field(default_factory=dict)


class BrokerAccountUpdate(BaseModel):
    account_label: str | None = None
    # Omit or {} to leave credentials untouched; a non-empty dict replaces
    # them wholesale (never merged -- a stale leftover field from an old
    # credential shape should never silently survive an edit).
    credentials: dict[str, Any] | None = None


class KiteLoginUrlOut(BaseModel):
    login_url: str


class KiteCallbackIn(BaseModel):
    request_token: str


class HDFCLoginUrlOut(BaseModel):
    login_url: str


class HDFCCallbackIn(BaseModel):
    auth_code: str


class BrokerBalanceOut(BaseModel):
    available_margin: float
    used_margin: float
    currency: str
