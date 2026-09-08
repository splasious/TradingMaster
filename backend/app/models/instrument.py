import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Instrument(Base):
    """Tradeable instrument catalog (PRD section 8/42.3).

    `data_source` identifies which adapter/broker backs an instrument's
    candles -- "delta_exchange" (services/market_data/delta_source.py) or
    "zerodha_kite" (the Data Backfill Platform's Zerodha block, bridged in
    via services/backfill_platform/catalog_sync.py). `external_ref` is the
    symbol that source's own API expects (e.g. "RELIANCE" or "NIFTY 50").
    A row can also carry a stale data_source left over from a retired
    source with no adapter anymore ("yahoo_nse") -- such rows are inert
    (hidden app-wide) until re-synced from a live source.

    `expiry`/`strike`/`option_type`/`lot_size`/`underlying_instrument_id`
    are F&O-only, always null for equity/index/perpetual_future rows.
    `option_type` is "CE"/"PE" for an option, null for a future (a future
    has an expiry but no strike/option_type). `underlying_instrument_id`
    points at the equity/index row a derivative is written against (e.g.
    an NFO NIFTY option -> the "NIFTY 50" index row) -- nullable since it's
    only resolvable when the underlying itself already exists as a
    synced Instrument.
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
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[float | None] = mapped_column(Float)
    option_type: Mapped[str | None] = mapped_column(String(2))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    underlying_instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("instruments.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("exchange", "symbol", name="uq_instruments_exchange_symbol"),)
