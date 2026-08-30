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
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class BackfillSource(str, enum.Enum):
    YAHOO = "yahoo"
    DELTA = "delta"
    ZERODHA = "zerodha"


class BfBackfillStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BfSymbol(Base):
    """A symbol tracked within this module, scoped to exactly one source --
    the same real-world instrument tracked from two sources is deliberately
    two separate rows here (e.g. NSE RELIANCE via Yahoo vs via Zerodha),
    each with its own bar history, never merged."""

    __tablename__ = "bf_symbols"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)  # source-native format
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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

    symbol: Mapped["BfSymbol"] = relationship(back_populates="bars")

    __table_args__ = (UniqueConstraint("symbol_id", "timeframe", "ts", name="uq_bf_bar_symbol_timeframe_ts"),)


class BfBackfillJob(Base):
    __tablename__ = "bf_backfill_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bf_symbols.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
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
