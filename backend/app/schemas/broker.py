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

    model_config = {"from_attributes": True}


class BrokerAccountCreate(BaseModel):
    broker_code: str
    account_label: str
    environment: str = "paper"
    credentials: dict[str, Any] = Field(default_factory=dict)


class KiteLoginUrlOut(BaseModel):
    login_url: str


class KiteCallbackIn(BaseModel):
    request_token: str
