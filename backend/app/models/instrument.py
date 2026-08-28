import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Instrument(Base):
    """Tradeable instrument catalog (PRD section 8/42.3).

    Phase 2 supports one real data source: NSE equities/indices via the
    local nse-yahoo-data service (data_source="yahoo_nse", external_ref is
    the NSE code that service's API expects, e.g. "RELIANCE" or "NIFTY 50").
    Later phases add broker-sourced instruments (Zerodha, Delta) the same
    way -- a new data_source value and adapter, no schema change.
    """

    __tablename__ = "instruments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(20), nullable=False, default="equity")
    data_source: Mapped[str] = mapped_column(String(30), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("exchange", "symbol", name="uq_instruments_exchange_symbol"),)
