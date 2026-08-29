import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AlertSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# PRD section 38's list, plus a couple of real events this codebase actually
# generates that the PRD's list doesn't name explicitly (kill switch,
# strategy activation) -- alert_type is a free string, not a DB enum, so
# adding one doesn't need a migration.
class AlertType(str, enum.Enum):
    STRATEGY_SIGNAL = "strategy_signal"
    ORDER_EXECUTED = "order_executed"
    ORDER_REJECTED = "order_rejected"
    STOP_LOSS_TRIGGERED = "stop_loss_triggered"
    TARGET_TRIGGERED = "target_triggered"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    DRAWDOWN_LIMIT = "drawdown_limit"
    BROKER_DISCONNECTED = "broker_disconnected"
    DATA_DISCONNECTED = "data_disconnected"
    STRATEGY_STOPPED = "strategy_stopped"
    SYSTEM_ERROR = "system_error"
    KILL_SWITCH_ACTIVATED = "kill_switch_activated"


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"))
    alert_type: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(50))
    object_id: Mapped[str | None] = mapped_column(String(50))
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
