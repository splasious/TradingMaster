from datetime import date

from pydantic import BaseModel


class InstrumentOut(BaseModel):
    id: str
    exchange: str
    symbol: str
    name: str
    instrument_type: str
    data_source: str
    is_active: bool
    expiry: date | None = None
    strike: float | None = None
    option_type: str | None = None
    lot_size: int | None = None
    underlying_instrument_id: str | None = None

    model_config = {"from_attributes": True}


class InstrumentSyncResult(BaseModel):
    data_source: str
    found: int
    created: int
    skipped: int
