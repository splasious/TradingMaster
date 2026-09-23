import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DeploymentStatus(str, enum.Enum):
    ACTIVE = "active"
    STOPPED = "stopped"


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, enum.Enum):
    FILLED = "filled"
    REJECTED = "rejected"


class PaperPortfolio(Base):
    """A named, currency-scoped capital pool (PRD section 21). A user can
    have several -- e.g. one INR pool for NSE strategies, one USD pool for
    Delta Exchange strategies -- each tracked independently with no FX
    conversion between them. A default pool is created lazily the first
    time a user with none calls GET /portfolios."""

    __tablename__ = "paper_portfolios"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False, default="Default")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaperDeployment(Base):
    __tablename__ = "paper_deployments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False, default="1d")
    status: Mapped[str] = mapped_column(String(20), default=DeploymentStatus.ACTIVE.value, nullable=False)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The outcome of the most recent evaluation -- "BUY"/"SELL"/"HOLD" when
    # a signal was actually computed, else the EvaluationOutcome's own
    # action word ("skipped", "error", ...) so this never sits blank while
    # last_evaluated_at is advancing; set from engine.py's evaluate_deployment
    # on every attempt, not just a successful signal. last_signal_reason
    # carries the human-readable detail (e.g. why it was skipped) for the
    # same evaluation.
    last_signal: Mapped[str | None] = mapped_column(String(20))
    last_signal_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperPosition(Base):
    """At most one open position per deployment -- same one-position-at-a-
    time model the backtest engine uses, for consistency between backtested
    and paper-traded behavior."""

    __tablename__ = "paper_positions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("paper_deployments.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    avg_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    # "long" | "short" -- mirrors the backtest engine's position["side"]
    # (services/backtest/engine.py), ported here so a Python strategy can
    # actually open a short (sell-to-open), not just close a long. Default
    # keeps every pre-existing row (all long, the only kind that could
    # exist before this column did) unchanged.
    side: Mapped[str] = mapped_column(String(10), nullable=False, default="long", server_default="long")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("paper_deployments.id", ondelete="CASCADE"), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("paper_deployments.id", ondelete="CASCADE"), nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(20), nullable=False, default="signal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaperNativeDeployment(Base):
    """A deployment for a `Strategy.code_type == "native"` strategy --
    trusted, unsandboxed Python that can't be expressed as one
    `generate_signal(candles, params)` call against one pre-selected
    instrument (see services/paper_trading/native_runner.py). Unlike
    `PaperDeployment`, deliberately has no `instrument_id` (the strategy
    picks its own instrument(s) live, e.g. a fresh ATM option strike
    every day) and no position-sizing config (the strategy sizes itself).

    `state` is an arbitrary JSON blob the strategy's own `evaluate(ctx)`
    reads and writes between ticks (ctx.state) -- e.g. for a 2-leg
    options spread, the currently-open position's legs and bias, or
    `null` when flat. Kept generic (not strategy-specific columns) so
    the next native strategy doesn't need a schema change."""

    __tablename__ = "paper_native_deployments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("paper_portfolios.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default=DeploymentStatus.ACTIVE.value, nullable=False)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_signal: Mapped[str | None] = mapped_column(String(20))
    last_signal_reason: Mapped[str | None] = mapped_column(String(500))
    state: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# A strategy's own exit_reason string ("time_cutoff_3pm", "pcr_exit_0.845",
# ...) -- record_trade() truncates to this rather than let an over-long
# reason fail the insert and lose the trade.
NATIVE_EXIT_REASON_MAX_LEN = 100


class PaperNativeTrade(Base):
    """Closed-trade ledger for native deployments -- `legs` is a JSON
    list of `{instrument_id, side, quantity, entry_price, exit_price}`
    so this works for a 1-leg or N-leg strategy alike without a schema
    change, the same "generic table, strategy-specific content" choice
    `PaperNativeDeployment.state` makes. Each leg also carries a snapshot
    of its contract (symbol, strike, option_type, expiry, lot_size,
    underlying_symbol -- see paper_trading/trade_record.py) so the row
    stays readable after the contract leaves the instrument catalog.

    `pnl` is gross -- exactly what moved through the pool's cash.
    `charges` is the estimated brokerage + statutory levies for the round
    trip (None where not estimable, and for rows saved before it existed);
    net P&L is pnl - charges."""

    __tablename__ = "paper_native_trades"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("paper_native_deployments.id", ondelete="CASCADE"), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    legs: Mapped[list] = mapped_column(JSON, nullable=False)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    charges: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(NATIVE_EXIT_REASON_MAX_LEN), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
