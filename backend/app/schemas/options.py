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


class OptionLegOut(BaseModel):
    instrument_id: str
    symbol: str
    ltp: float | None
    # Change since this leg's own first candle of its latest trading day --
    # NOT Kite's "vs previous session close" convention, see chain.py.
    ltp_change: float | None
    open_interest: float | None
    open_interest_change: float | None
    as_of: datetime


class ChainRowOut(BaseModel):
    strike: float
    call: OptionLegOut | None
    put: OptionLegOut | None


class HistoryDepthOut(BaseModel):
    symbol: str | None
    # What THIS app has already backfilled into ohlcv_candles.
    our_earliest: datetime | None
    our_latest: datetime | None
    our_candle_count: int
    # What Kite's own historical API actually reports right now, queried
    # live through the connected Zerodha session -- None if no account is
    # connected or the live probe itself failed (see `error`).
    kite_earliest: datetime | None
    kite_latest: datetime | None
    kite_candle_count: int | None
    error: str | None
