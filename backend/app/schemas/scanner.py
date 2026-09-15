from typing import Literal

from pydantic import BaseModel

from app.schemas.instrument import InstrumentOut

Operator = Literal[">", "<", ">=", "<=", "=="]

RAW_FIELDS = ("open", "high", "low", "close", "volume")


class ScanCondition(BaseModel):
    field: str  # a raw OHLCV field, or "{indicator_code}" / "{indicator_code}.{output}"
    operator: Operator
    value: float


class ScanRequest(BaseModel):
    exchange: str | None = None
    timeframe: str = "1d"
    conditions: list[ScanCondition]


class ScanMatch(BaseModel):
    instrument: InstrumentOut
    values: dict[str, float | None]


class ScanResponse(BaseModel):
    matched: list[ScanMatch]
    scanned_count: int


class SavedScanCreate(BaseModel):
    name: str
    exchange: str | None = None
    timeframe: str = "1d"
    conditions: list[ScanCondition]


class SavedScanOut(BaseModel):
    id: str
    name: str
    exchange: str | None
    timeframe: str
    conditions: list[ScanCondition]


class StrategyScanRequest(BaseModel):
    strategy_id: str
    exchange: str | None = None


class StrategySignalMatch(BaseModel):
    instrument: InstrumentOut
    signal: str


class StrategyScanResponse(BaseModel):
    matched: list[StrategySignalMatch]
    scanned_count: int
    skipped_symbols: list[str]
    # The strategy version's own parameters (e.g. rsi_period, long_rsi_level)
    # and the values actually used to compute every signal in `matched` --
    # so the scan result is self-describing rather than making the viewer
    # go check the Strategy Builder separately to know what was run.
    strategy_version_number: int
    parameters: dict[str, float]
