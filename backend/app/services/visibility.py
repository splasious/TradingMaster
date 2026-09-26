"""What the site shows. Delta Exchange is hidden: its instruments, backfilled
candles, watchlists and settings stay in the database untouched, but no API
lists, serves or accepts them, and its broker can't be connected.
show_delta_exchange (SHOW_DELTA_EXCHANGE in .env) brings it back,
together with DELTA_VISIBLE in frontend/lib/features.ts."""

from sqlalchemy import and_, true

from app.core.config import get_settings
from app.models.instrument import Instrument

DELTA_EXCHANGE = "DELTA"  # instruments.exchange
DELTA_DATA_SOURCE = "delta_exchange"  # instruments.data_source
DELTA_BF_SOURCE = "delta"  # bf_symbols.source, bf_backfill_jobs.source
DELTA_BROKER_CODE = "delta_exchange"  # brokers.code


def delta_visible() -> bool:
    return get_settings().show_delta_exchange


def hidden_bf_sources() -> tuple[str, ...]:
    return () if delta_visible() else (DELTA_BF_SOURCE,)


def hidden_broker_codes() -> tuple[str, ...]:
    return () if delta_visible() else (DELTA_BROKER_CODE,)


def bf_source_visible(source: str) -> bool:
    return source not in hidden_bf_sources()


def broker_visible(code: str) -> bool:
    return code not in hidden_broker_codes()


def instrument_visible(instrument: Instrument) -> bool:
    return delta_visible() or (
        instrument.exchange != DELTA_EXCHANGE and instrument.data_source != DELTA_DATA_SOURCE
    )


def visible_instruments():
    """WHERE clause for a select over Instrument."""
    if delta_visible():
        return true()
    return and_(Instrument.exchange != DELTA_EXCHANGE, Instrument.data_source != DELTA_DATA_SOURCE)
