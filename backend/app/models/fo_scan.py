"""Data behind the F&O opening-momentum scan (FLY OI SCN).

fo_oi_snapshots: the open interest of every contract the scan's Total OI
adds up -- each F&O stock's current-month future and all of its CE and PE
strikes -- at the day's close (15:31, the next session's "yesterday"), at
the pre-open backup (09:10, only when the close was missed) and at 09:20.
Only the last two sessions are kept: the scan compares today with
yesterday's close and needs nothing older.

fo_oi_totals: the same readings added up per stock (future, CE, PE),
kept for good -- the OI history a backtest of the scan needs, which Kite
can't supply once the contracts have expired.

fo_scan_results: one row per stock per scan (09:20 and 09:25) -- the move,
the Total OI change, each rule passed or failed, and what came of it
(shortlisted, traded, P&L). Kept for good.

Written by services/fo_scan/oi_store.py and the strategy
(services/strategy/native_strategies/fo_opening_momentum.py).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MARK_CLOSE = "close"
MARK_PRE_OPEN = "pre_open"
MARK_0920 = "09:20"


class FoOiSnapshot(Base):
    __tablename__ = "fo_oi_snapshots"
    __table_args__ = (
        UniqueConstraint("session_date", "mark", "instrument_id", name="uq_fo_oi_snapshots_session_mark_instrument"),
        Index("ix_fo_oi_snapshots_underlying_session", "underlying_id", "session_date", "mark"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    mark: Mapped[str] = mapped_column(String(10), nullable=False)
    underlying_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(3), nullable=False)  # FUT | CE | PE
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[float | None] = mapped_column(Float)
    oi: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    last_price: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="quote")  # quote | daily_candle
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FoOiTotal(Base):
    __tablename__ = "fo_oi_totals"
    __table_args__ = (
        UniqueConstraint("session_date", "mark", "symbol", "expiry", name="uq_fo_oi_totals_session_mark_symbol_expiry"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    mark: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    underlying_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="SET NULL"))
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    fut_oi: Mapped[float | None] = mapped_column(Float)
    ce_oi: Mapped[float | None] = mapped_column(Float)
    pe_oi: Mapped[float | None] = mapped_column(Float)
    total_oi: Mapped[float | None] = mapped_column(Float)
    contracts_listed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contracts_with_oi: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FoScanResult(Base):
    __tablename__ = "fo_scan_results"
    __table_args__ = (
        UniqueConstraint("deployment_id", "session_date", "scan", "symbol", name="uq_fo_scan_results_deployment_session_scan_symbol"),
        Index("ix_fo_scan_results_session", "session_date", "scan"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("paper_native_deployments.id", ondelete="SET NULL"))
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    scan: Mapped[str] = mapped_column(String(5), nullable=False)  # "09:20" | "09:25"
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    underlying_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="SET NULL"))
    direction: Mapped[str | None] = mapped_column(String(2))  # CE (gainer) | PE (loser)

    prev_close: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    move_pct: Mapped[float | None] = mapped_column(Float)

    oi_baseline: Mapped[str | None] = mapped_column(String(20))  # close | pre_open | daily_candle
    oi_prev_total: Mapped[float | None] = mapped_column(Float)
    oi_now_total: Mapped[float | None] = mapped_column(Float)
    oi_change_pct: Mapped[float | None] = mapped_column(Float)
    fut_prev: Mapped[float | None] = mapped_column(Float)
    fut_now: Mapped[float | None] = mapped_column(Float)
    ce_prev: Mapped[float | None] = mapped_column(Float)
    ce_now: Mapped[float | None] = mapped_column(Float)
    pe_prev: Mapped[float | None] = mapped_column(Float)
    pe_now: Mapped[float | None] = mapped_column(Float)
    legs_counted: Mapped[int | None] = mapped_column(Integer)
    legs_listed: Mapped[int | None] = mapped_column(Integer)

    retrace_pct: Mapped[float | None] = mapped_column(Float)
    passed_move: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    passed_oi: Mapped[bool | None] = mapped_column(Boolean)
    passed_retrace: Mapped[bool | None] = mapped_column(Boolean)
    nifty_bias: Mapped[str | None] = mapped_column(String(12))  # green | red | unavailable
    passed_nifty: Mapped[bool | None] = mapped_column(Boolean)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    breakout_high: Mapped[float | None] = mapped_column(Float)
    breakout_low: Mapped[float | None] = mapped_column(Float)
    option_symbol: Mapped[str | None] = mapped_column(String(50))
    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_premium: Mapped[float | None] = mapped_column(Float)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_premium: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(100))
    pnl: Mapped[float | None] = mapped_column(Float)

    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
