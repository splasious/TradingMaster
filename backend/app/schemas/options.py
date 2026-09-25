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


class EffectivePcrOut(BaseModel):
    """The single PCR number a PCR-driven native strategy actually decides
    its bias from (see services/options/pcr.py::compute_effective_pcr) --
    summed put/call OI across the nearest `num_expiries` live expiries,
    not a per-expiry series."""

    underlying_symbol: str
    num_expiries: int
    timeframe: str
    pcr: float | None
    bias: str  # "bearish" | "bullish" | "neutral" | "unavailable" (pcr is None)
    # The underlying's live tick, straight from TickEngine (same source the
    # native PCR strategy itself reads via ctx.get_price) -- None if the
    # underlying isn't found or has no live tick on file yet.
    spot_price: float | None


class PcrSnapshotExpiryOut(BaseModel):
    expiry: date
    atm_strike: float | None
    strike_lo: float | None
    strike_hi: float | None
    contracts_expected: int
    contracts_with_oi: int
    total_call_oi: float | None
    total_put_oi: float | None
    pcr: float | None
    call_oi_change: float | None
    put_oi_change: float | None
    oi_change_pcr: float | None
    call_oi_change_day: float | None
    put_oi_change_day: float | None


class PcrSnapshotRowOut(BaseModel):
    """One 15-minute mark (services/options/pcr_snapshots.py). `status` is
    "recorded", or "missing"/"pending" (no record; pending while the
    mark's live capture can still land) -- every other field is then None."""

    ts: datetime
    session_date: date
    status: str
    source: str | None = None
    captured_at: datetime | None = None
    expiries: list[date] = []
    spot: float | None = None
    atm_strike: float | None = None
    strike_step: float | None = None
    strike_window: int | None = None
    contracts_expected: int | None = None
    contracts_with_oi: int | None = None
    total_call_oi: float | None = None
    total_put_oi: float | None = None
    pcr: float | None = None
    prev_ts: datetime | None = None
    prev_pcr: float | None = None
    pcr_change: float | None = None
    spot_change: float | None = None
    spot_change_pct: float | None = None
    atm_shift: float | None = None
    call_oi_change: float | None = None
    put_oi_change: float | None = None
    oi_change_pcr: float | None = None
    oi_change_contracts: int | None = None
    day_baseline_ts: datetime | None = None
    call_oi_change_day: float | None = None
    put_oi_change_day: float | None = None
    oi_change_pcr_day: float | None = None
    positioning: str | None = None
    oi_driver: str | None = None
    flags: list[str] = []
    expiry_rows: list[PcrSnapshotExpiryOut] | None = None


class PcrCaptureStatusOut(BaseModel):
    running: bool
    filling: bool
    last_capture_ts: datetime | None
    last_capture_at: datetime | None
    last_error: str | None
    last_fill_at: datetime | None
    last_fill_result: dict | None
    last_fill_error: str | None


class PcrSnapshotsOut(BaseModel):
    underlying: str
    strike_window: int
    expiries_summed: int
    marks_per_session: int
    rows: list[PcrSnapshotRowOut]  # newest first
    capture: PcrCaptureStatusOut


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
