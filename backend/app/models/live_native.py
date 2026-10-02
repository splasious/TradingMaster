"""Native strategies trading live (services/live_trading/native_live.py).

The paper side's PaperNativeDeployment/PaperNativeTrade, for real money: a
strategy's code runs unchanged, but every leg it opens or closes is a real
market order on the broker account it was started on. What the broker
actually holds for it -- real quantities at real fill prices -- is kept in
LiveNativePosition, apart from the strategy's own state (which still holds
its paper-sized view: a live run trades `lots_per_leg` lots whatever the
code's own size).
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

LIVE_NATIVE_ACTIVE = "active"
LIVE_NATIVE_PAUSED = "paused"
LIVE_NATIVE_STOPPED = "stopped"

PRODUCT_INTRADAY = "intraday"  # MIS -- the broker squares it off near the close
PRODUCT_OVERNIGHT = "overnight"  # NRML for F&O, CNC for stocks


class LiveNativeDeployment(Base):
    __tablename__ = "live_native_deployments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    strategy_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False)
    # The paper run whose card it was switched on from (the Trading page's
    # Paper / Live switch, agreed 2 Oct): paper keeps running beside it.
    paper_deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("paper_native_deployments.id", ondelete="SET NULL"), index=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=LIVE_NATIVE_ACTIVE)  # active | paused | stopped
    state: Mapped[dict | None] = mapped_column(JSON)  # the strategy's own, as on paper

    # Size and limits, set when it's started (editable on its card).
    lots_per_leg: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    capital: Mapped[float] = mapped_column(Float, nullable=False)  # the capital cap
    daily_loss_limit: Mapped[float | None] = mapped_column(Float)  # None: DEFAULT_DAILY_LOSS_PCT of capital
    max_orders_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    product_style: Mapped[str] = mapped_column(String(20), nullable=False, default=PRODUCT_OVERNIGHT)

    pause_reason: Mapped[str | None] = mapped_column(String(500))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A pause that lifts by itself (the daily loss limit: back at the next session's open).
    resume_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_signal: Mapped[str | None] = mapped_column(String(20))
    last_signal_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiveNativePosition(Base):
    """What the broker holds for one deployment in one instrument: signed
    quantity (+ long, - short) at the average fill price, and the quantity
    the strategy itself thinks it holds (its own size), so its closes can be
    scaled to the real one."""

    __tablename__ = "live_native_positions"
    __table_args__ = (UniqueConstraint("deployment_id", "instrument_id", name="uq_live_native_position"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("live_native_deployments.id", ondelete="CASCADE"), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    avg_price: Mapped[float] = mapped_column(Float, nullable=False)
    strategy_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    product: Mapped[str | None] = mapped_column(String(10))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LiveNativeTrade(Base):
    """A closed round trip at the broker's real fill prices -- the live
    twin of PaperNativeTrade (same legs shape)."""

    __tablename__ = "live_native_trades"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("live_native_deployments.id", ondelete="CASCADE"), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    legs: Mapped[list] = mapped_column(JSON, nullable=False)
    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    charges: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LiveRiskSettings(Base):
    """Per user: the account-wide daily loss limit across all their live
    native strategies. None: DEFAULT_ACCOUNT_LOSS_PCT of their live capital."""

    __tablename__ = "live_risk_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    account_daily_loss_limit: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class LiveAccountBaseline(Base):
    """What a broker account already held of a contract -- yours, not a
    strategy's -- when a live strategy first traded it there (agreed 1 Oct
    2026). Reconciliation expects this plus what the strategies hold; it is
    recorded afresh whenever no strategy on the account holds the contract,
    so it only counts while one does. `contract_key` is the broker's own
    (native_gateway.BrokerGateway.key), joined as "segment|id"."""

    __tablename__ = "live_account_baselines"
    __table_args__ = (UniqueConstraint("broker_account_id", "contract_key", name="uq_live_account_baseline"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False)
    contract_key: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
