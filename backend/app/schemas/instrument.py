from pydantic import BaseModel


class InstrumentOut(BaseModel):
    id: str
    exchange: str
    symbol: str
    name: str
    instrument_type: str
    data_source: str
    is_active: bool

    model_config = {"from_attributes": True}


class InstrumentSyncResult(BaseModel):
    data_source: str
    found: int
    created: int
    skipped: int
