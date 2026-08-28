import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.services.strategy.state_machine import StrategyStatus


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    owner_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_type: Mapped[str] = mapped_column(String(10), nullable=False)  # "visual" | "python"
    status: Mapped[str] = mapped_column(String(30), default=StrategyStatus.DRAFT.value, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    versions: Mapped[list["StrategyVersion"]] = relationship(
        back_populates="strategy", cascade="all, delete-orphan", order_by="StrategyVersion.version_number"
    )


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False, default="1d")
    instrument_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    entry_rules: Mapped[dict | None] = mapped_column(JSON)
    exit_rules: Mapped[dict | None] = mapped_column(JSON)
    python_code: Mapped[str | None] = mapped_column(Text)
    position_sizing: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    risk_rules: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    strategy: Mapped["Strategy"] = relationship(back_populates="versions")
