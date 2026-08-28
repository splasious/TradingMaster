import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Environment(str, enum.Enum):
    PAPER = "paper"
    LIVE = "live"


class ConnectionStatus(str, enum.Enum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    RECONNECTING = "reconnecting"
    DISCONNECTED = "disconnected"
    DELAYED = "delayed"
    ERROR = "error"


class Broker(Base):
    """Catalog of supported broker/exchange integrations (PRD section 7)."""

    __tablename__ = "brokers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # e.g. "zerodha_kite"
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    accounts: Mapped[list["BrokerAccount"]] = relationship(back_populates="broker")


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    broker_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("brokers.id", ondelete="CASCADE"), nullable=False)
    account_label: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), default=Environment.PAPER.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    broker: Mapped["Broker"] = relationship(back_populates="accounts")
    credential: Mapped["BrokerCredential | None"] = relationship(
        back_populates="broker_account", cascade="all, delete-orphan", uselist=False
    )
    connection: Mapped["BrokerConnection | None"] = relationship(
        back_populates="broker_account", cascade="all, delete-orphan", uselist=False
    )


class BrokerCredential(Base):
    """Credentials are stored only as a Fernet-encrypted blob (see
    app.core.encryption). Plaintext values never touch the database, logs,
    or API responses (PRD section 47)."""

    __tablename__ = "broker_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("broker_accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    encrypted_payload: Mapped[str] = mapped_column(String(4000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    broker_account: Mapped["BrokerAccount"] = relationship(back_populates="credential")


class BrokerConnection(Base):
    __tablename__ = "broker_connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("broker_accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default=ConnectionStatus.DISCONNECTED.value, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))

    broker_account: Mapped["BrokerAccount"] = relationship(back_populates="connection")
