"""Data Backfill Platform (separate PRD: 'TradingMaster - Data Backfill
Platform'). Deliberately its own schema, not a reuse of `instruments` /
`ohlcv_candles` -- those are shared by strategies, backtesting, paper and
live trading, and this PRD's own non-goal is explicit: "No cross-source
data merging/normalization in v1 (each source's data stays in its own
schema)". Bolting three sources' history onto one instrument row would
mean the first source to backfill a given day silently wins that slot
(ohlcv_candles' uniqueness is instrument+timeframe+ts, not source-aware) --
exactly the kind of cross-source interference this PRD asks to avoid.
"""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Index,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class BackfillSource(str, enum.Enum):
    DELTA = "delta"
    ZERODHA = "zerodha"
    ZERODHA_NFO = "zerodha_nfo"


class BfBackfillStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Queue order: a job someone started by hand runs before bulk work, and a
# bulk backfill before the daily automatic top-up. A top-up adds the
# timeframe's place in TOPUP_TIMEFRAME_ORDER, so daily bars come in first.
JOB_PRIORITY_MANUAL = 0
JOB_PRIORITY_BULK = 10
JOB_PRIORITY_SCHEDULED = 20
TOPUP_TIMEFRAME_ORDER = ["1d", "60m", "30m", "15m", "5m", "1m"]


class BfSymbol(Base):
    """A symbol tracked within this module, scoped to exactly one source --
    the same real-world instrument tracked from two sources is deliberately
    two separate rows here, each with its own bar history, never merged."""

    __tablename__ = "bf_symbols"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)  # source-native format
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set by CatalogSyncScheduler after it last copied this symbol's bars
    # into the main Instrument/OhlcvCandle schema. NULL means never synced.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # F&O metadata (source="zerodha_nfo" only) -- captured once, at
    # get_or_create_symbol time, straight from Kite's NFO instrument dump
    # row the user searched/selected. Carried here (not re-fetched live at
    # sync time) because catalog_sync_scheduler's background tick has no
    # per-user Kite session to call back into -- see catalog_sync.py.
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[float | None] = mapped_column(Float)
    option_type: Mapped[str | None] = mapped_column(String(2))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    underlying_symbol: Mapped[str | None] = mapped_column(String(50))  # Kite's NFO "name" column, e.g. "NIFTY"

    bars: Mapped[list["BfOhlcvBar"]] = relationship(back_populates="symbol", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("source", "symbol", name="uq_bf_symbol_source_symbol"),)


class BfOhlcvBar(Base):
    __tablename__ = "bf_ohlcv_bars"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bf_symbols.id", ondelete="CASCADE"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)  # F&O only, see OhlcvCandle's own field

    symbol: Mapped["BfSymbol"] = relationship(back_populates="bars")

    __table_args__ = (UniqueConstraint("symbol_id", "timeframe", "ts", name="uq_bf_bar_symbol_timeframe_ts"),)


class BfBackfillJob(Base):
    __tablename__ = "bf_backfill_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bf_symbols.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default=BfBackfillStatus.PENDING.value, nullable=False)
    downloaded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The database is the queue (BackfillWorker): jobs wait here as
    # "pending" -- surviving a restart -- and run one at a time, lowest
    # priority first, not before run_after (a retry's back-off).
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=JOB_PRIORITY_MANUAL, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("bf_backfill_runs.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("ix_bf_backfill_jobs_status_priority", "status", "priority", "created_at"),
        Index("ix_bf_backfill_jobs_symbol_id", "symbol_id"),
        Index("ix_bf_backfill_jobs_run_id", "run_id"),
    )


class BfBackfillRun(Base):
    """One top-up: the daily automatic run for a source, or one started with
    "Top up now". Its jobs carry its id; progress is counted from them."""

    __tablename__ = "bf_backfill_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # "scheduled" | "manual"
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # The NSE session this run brings the data up to.
    session_date: Mapped[date | None] = mapped_column(Date)
    # "waiting_login" -> "running" -> "completed" | "cancelled"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    jobs_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(String(300))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BfCoverage(Base):
    """What is saved for one symbol and timeframe: the watermark the daily
    top-up fetches after, and what every "saved up to" status reads --
    instead of aggregating bf_ohlcv_bars (20M+ rows) on each request.
    Kept current by every save (see coverage.py)."""

    __tablename__ = "bf_coverage"

    symbol_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bf_symbols.id", ondelete="CASCADE"), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(10), primary_key=True)
    first_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Bars on the last saved (IST) day -- fewer than a full session's worth
    # marks a partial day.
    last_day_bars: Mapped[int | None] = mapped_column(Integer)
    # The last session a job successfully fetched through, bars or not: a
    # contract with no trades that day has nothing to save but is not behind.
    checked_through: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


DEFAULT_TOPUP_TIMEFRAMES = ["1m", "5m", "15m", "30m", "60m", "1d"]


class BfSettings(Base):
    """The Data Backfill page's schedule settings -- a single row (id=1)."""

    __tablename__ = "bf_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    auto_topup_zerodha: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_topup_zerodha_nfo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Delta Exchange paused: no live sync, no automatic top-up; saved data kept.
    delta_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    topup_time: Mapped[str] = mapped_column(String(5), nullable=False, default="16:15")  # IST, HH:MM
    # The live candle sync's daily window on NSE trading days (IST, HH:MM) --
    # it keeps running a few minutes past the end to save the final candles.
    live_start: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    live_end: Mapped[str] = mapped_column(String(5), nullable=False, default="15:30")
    topup_timeframes: Mapped[list] = mapped_column(JSON, nullable=False, default=lambda: list(DEFAULT_TOPUP_TIMEFRAMES))
    # Set once bf_coverage has been built from the stored bars (coverage.py);
    # null means the build hasn't finished, so it runs again at startup.
    coverage_built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BfWatchlist(Base):
    __tablename__ = "bf_watchlists"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    items: Mapped[list["BfWatchlistItem"]] = relationship(back_populates="watchlist", cascade="all, delete-orphan")


class BfWatchlistItem(Base):
    __tablename__ = "bf_watchlist_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    watchlist_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bf_watchlists.id", ondelete="CASCADE"), nullable=False)
    symbol_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bf_symbols.id", ondelete="CASCADE"), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    watchlist: Mapped["BfWatchlist"] = relationship(back_populates="items")
    symbol: Mapped["BfSymbol"] = relationship()

    __table_args__ = (UniqueConstraint("watchlist_id", "symbol_id", name="uq_bf_watchlist_item"),)
