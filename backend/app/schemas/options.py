from datetime import date, datetime

from pydantic import BaseModel


class UnderlyingOut(BaseModel):
    instrument_id: str
    symbol: str


class ExpiryOut(BaseModel):
    expiry: date
    future_count: int
    option_count: int


class PcrPointOut(BaseModel):
    ts: datetime
    total_call_oi: float
    total_put_oi: float
    # None when total_call_oi is 0 -- the ratio is undefined, not zero.
    pcr: float | None
    # None on the series' first point -- there's no prior bar to diff against.
    call_oi_change: float | None
    put_oi_change: float | None
