"""NIFTY option PCR analysis: one record every 15 minutes, 09:00-15:30 IST
on NSE trading days, kept in full for history and backtests.

pcr_snapshots is the record the PCR Analysis page and strategies read: the
market as it was at `ts` -- NIFTY, ATM, total Call/Put OI over ATM ±40
strikes of the next 4 weekly expiries summed, PCR -- plus what changed since
the previous record (ΔOI, ΔOI PCR, ΔPCR, NIFTY move, ATM shift, positioning).

pcr_snapshot_expiries holds the same totals for each expiry separately
("current week only" or one expiry needs no recalculation).

pcr_strike_oi is every captured contract's OI at `ts` -- ATM ±60, wider than
the ±40 shown, so the next record's ΔOI can compare the very same contracts
even after the ATM moves, and so any window can be recomputed later.

Written by services/options/pcr_snapshots.py.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

SOURCE_LIVE = "live_quote"
SOURCE_HISTORICAL = "historical_fill"


class PcrSnapshot(Base):
    __tablename__ = "pcr_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    underlying: Mapped[str] = mapped_column(String(20), nullable=False)  # Kite name, e.g. "NIFTY"
    # The 15-minute mark the numbers are true at (09:00, 09:15 ... 15:30 IST).
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    strike_window: Mapped[int] = mapped_column(Integer, nullable=False)
    expiries: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # ISO dates, nearest first

    spot: Mapped[float | None] = mapped_column(Float)
    atm_strike: Mapped[float | None] = mapped_column(Float)
    strike_step: Mapped[float | None] = mapped_column(Float)
    contracts_expected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contracts_with_oi: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    total_call_oi: Mapped[float | None] = mapped_column(Float)
    total_put_oi: Mapped[float | None] = mapped_column(Float)
    pcr: Mapped[float | None] = mapped_column(Float)

    # Compared with the previous record (normally 15 minutes earlier; the
    # previous session's 15:30 for 09:00). ΔOI sums each contract's own
    # change, over contracts in both records -- `oi_change_contracts` of them.
    prev_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prev_pcr: Mapped[float | None] = mapped_column(Float)
    pcr_change: Mapped[float | None] = mapped_column(Float)
    spot_change: Mapped[float | None] = mapped_column(Float)
    spot_change_pct: Mapped[float | None] = mapped_column(Float)
    atm_shift: Mapped[float | None] = mapped_column(Float)
    call_oi_change: Mapped[float | None] = mapped_column(Float)
    put_oi_change: Mapped[float | None] = mapped_column(Float)
    oi_change_pcr: Mapped[float | None] = mapped_column(Float)  # None when Call ΔOI is 0
    oi_change_contracts: Mapped[int | None] = mapped_column(Integer)
    # Since the previous session's last record, the same way.
    day_baseline_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    call_oi_change_day: Mapped[float | None] = mapped_column(Float)
    put_oi_change_day: Mapped[float | None] = mapped_column(Float)
    oi_change_pcr_day: Mapped[float | None] = mapped_column(Float)

    # bullish / bearish / divergence / unwinding / flat, and what drove it
    # (put_led_buildup, put_buildup_call_unwinding, call_unwinding,
    # call_led_buildup, call_buildup_put_unwinding, put_unwinding,
    # both_unwinding, flat) -- see pcr_snapshots.classify.
    positioning: Mapped[str | None] = mapped_column(String(20))
    oi_driver: Mapped[str | None] = mapped_column(String(40))
    # pre_open, late, gap_before, expiry_rolled, low_coverage
    flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    calc_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    expiry_rows: Mapped[list["PcrSnapshotExpiry"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", order_by="PcrSnapshotExpiry.expiry"
    )

    __table_args__ = (
        UniqueConstraint("underlying", "ts", name="uq_pcr_snapshots_underlying_ts"),
        Index("ix_pcr_snapshots_underlying_session", "underlying", "session_date"),
    )


class PcrSnapshotExpiry(Base):
    __tablename__ = "pcr_snapshot_expiries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("pcr_snapshots.id", ondelete="CASCADE"), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    atm_strike: Mapped[float | None] = mapped_column(Float)
    strike_lo: Mapped[float | None] = mapped_column(Float)
    strike_hi: Mapped[float | None] = mapped_column(Float)
    contracts_expected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contracts_with_oi: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_call_oi: Mapped[float | None] = mapped_column(Float)
    total_put_oi: Mapped[float | None] = mapped_column(Float)
    pcr: Mapped[float | None] = mapped_column(Float)
    call_oi_change: Mapped[float | None] = mapped_column(Float)
    put_oi_change: Mapped[float | None] = mapped_column(Float)
    oi_change_pcr: Mapped[float | None] = mapped_column(Float)
    call_oi_change_day: Mapped[float | None] = mapped_column(Float)
    put_oi_change_day: Mapped[float | None] = mapped_column(Float)

    snapshot: Mapped[PcrSnapshot] = relationship(back_populates="expiry_rows")

    __table_args__ = (UniqueConstraint("snapshot_id", "expiry", name="uq_pcr_snapshot_expiries_snapshot_expiry"),)


class PcrStrikeOi(Base):
    __tablename__ = "pcr_strike_oi"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("pcr_snapshots.id", ondelete="CASCADE"), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)  # CE / PE
    tradingsymbol: Mapped[str] = mapped_column(String(50), nullable=False)
    # None when Kite returned nothing for the contract (it counts against
    # the record's coverage, never as 0).
    oi: Mapped[float | None] = mapped_column(Float)
    last_price: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("snapshot_id", "expiry", "strike", "option_type", name="uq_pcr_strike_oi_contract"),
    )
