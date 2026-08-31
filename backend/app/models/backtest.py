import enum
import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BacktestStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BacktestJob(Base):
    __tablename__ = "backtest_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0)
    # Overrides the strategy version's own position_sizing for this run only,
    # when both are set (e.g. try the same strategy at a different capital
    # allocation without editing it in Strategy Builder). Null means "use
    # whatever the strategy version already has" -- the original behavior.
    position_sizing_type: Mapped[str | None] = mapped_column(String(20))
    position_sizing_value: Mapped[float | None] = mapped_column(Float)
    brokerage_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.03)
    slippage_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)
    tax_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    out_of_sample_split_pct: Mapped[float | None] = mapped_column(Float)
    run_monte_carlo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), default=BacktestStatus.PENDING.value, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("backtest_jobs.id", ondelete="CASCADE"), unique=True, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)
    out_of_sample_metrics: Mapped[dict | None] = mapped_column(JSON)
    monte_carlo: Mapped[dict | None] = mapped_column(JSON)
    equity_curve: Mapped[list] = mapped_column(JSON, nullable=False)  # [[iso_ts, equity], ...]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("backtest_jobs.id", ondelete="CASCADE"), nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(20), nullable=False)


class OptimizationJob(Base):
    """Grid search over a Python strategy's `params` (PRD section 20).
    Visual-mode rule conditions use literal values, not named parameters,
    so this is Python-strategy only -- see optimization.py."""

    __tablename__ = "optimization_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0)
    param_ranges: Mapped[list] = mapped_column(JSON, nullable=False)  # [{name, min, max, step}, ...]
    rank_metric: Mapped[str] = mapped_column(String(30), nullable=False, default="sharpe_ratio")
    status: Mapped[str] = mapped_column(String(20), default=BacktestStatus.PENDING.value, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OptimizationResult(Base):
    __tablename__ = "optimization_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("optimization_jobs.id", ondelete="CASCADE"), unique=True, nullable=False)
    runs: Mapped[list] = mapped_column(JSON, nullable=False)  # [{params, metrics}, ...] ranked best-first
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
