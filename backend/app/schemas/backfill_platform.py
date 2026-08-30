from datetime import date, datetime

from pydantic import BaseModel


class SourceStatusOut(BaseModel):
    source: str
    connected: bool
    detail: str
    expires_at: datetime | None = None


class SymbolSearchResultOut(BaseModel):
    symbol: str
    display_name: str


class BfBackfillJobCreate(BaseModel):
    source: str
    symbol: str
    display_name: str
    timeframe: str
    start_date: date | None = None
    end_date: date | None = None


class BfBackfillJobOut(BaseModel):
    id: str
    symbol_id: str
    symbol: str
    display_name: str
    source: str
    timeframe: str
    start_date: date | None
    end_date: date | None
    status: str
    downloaded_count: int
    inserted_count: int
    duplicate_count: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class CompletenessSegmentOut(BaseModel):
    start: date
    end: date
    status: str


class CompletenessOut(BaseModel):
    segments: list[CompletenessSegmentOut]


class BfWatchlistCreate(BaseModel):
    name: str
    tags: list[str] = []


class BfWatchlistOut(BaseModel):
    id: str
    name: str
    tags: list[str]
    symbol_count: int
    never_backfilled_count: int
    last_backfill_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BfWatchlistItemOut(BaseModel):
    id: str
    symbol_id: str
    source: str
    symbol: str
    display_name: str
    bar_count: int
    last_job_status: str | None


class WatchlistItemAdd(BaseModel):
    source: str
    symbol: str
    display_name: str


class WatchlistImportRow(BaseModel):
    source: str
    symbol: str
    display_name: str = ""


class WatchlistImportResult(BaseModel):
    added: int
    skipped: int
