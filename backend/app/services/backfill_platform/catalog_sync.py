"""Bridges the Data Backfill Platform's isolated bf_* schema into the main
Instrument/OhlcvCandle schema that Charts, Strategy Builder, Backtesting,
and Optimization actually read from. The two schemas are deliberately kept
separate while backfilling (per that module's own "no cross-source
merging" rule) -- this is the bridge between them, run either on demand
(the "Sync" buttons) or continuously by CatalogSyncScheduler. Never
overwrites a candle the main catalog already has; a bar already present
for (instrument, timeframe, ts) is left alone."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle

_SOURCE_TO_EXCHANGE = {"delta": "DELTA", "zerodha": "NSE", "zerodha_nfo": "NFO"}
_SOURCE_TO_DATA_SOURCE = {"delta": "delta_exchange", "zerodha": "zerodha_kite", "zerodha_nfo": "zerodha_kite"}

# A handful of NFO index underlyings whose Kite "name" doesn't match the
# seeded index Instrument's own symbol verbatim (Kite: "NIFTY", this app's
# seeded row: "NIFTY 50") -- best-effort resolution only, see
# _resolve_underlying's docstring for what happens when nothing matches.
# Shared (not module-private) since nfo_expiry_rotation.py also needs it,
# in reverse, to go from our Instrument.symbol back to Kite's own "name".
UNDERLYING_NAME_ALIASES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK"}


class CatalogSyncError(Exception):
    """Raised when a bf_symbol's source has no main-catalog mapping. Every
    real source (delta/zerodha) has one; this now only fires for a source
    added to _VALID_SOURCES without a matching entry here."""


@dataclass
class CatalogSyncResult:
    symbol: str
    instrument_id: str
    instrument_created: bool
    bars_synced: int
    bars_skipped: int


async def _resolve_underlying(db: AsyncSession, kite_name: str | None) -> uuid.UUID | None:
    """Best-effort match of an NFO row's Kite `name` (e.g. "NIFTY",
    "RELIANCE") against an already-synced equity/index Instrument's own
    `symbol` -- exact match first, then the small known-alias table for
    index names that don't match verbatim. Returns None (never raises) if
    nothing matches yet, e.g. the underlying hasn't been synced from its
    own NSE source first -- `underlying_instrument_id` is nullable exactly
    for this reason, not a hard dependency ordering."""
    if not kite_name:
        return None
    candidates = [kite_name, UNDERLYING_NAME_ALIASES.get(kite_name, kite_name)]
    for candidate in candidates:
        match = (
            await db.execute(select(Instrument.id).where(Instrument.exchange == "NSE", Instrument.symbol == candidate))
        ).scalar_one_or_none()
        if match is not None:
            return match
    return None


def _instrument_type_for(bf_symbol: BfSymbol) -> str:
    if bf_symbol.source == "delta":
        return "perpetual_future"
    if bf_symbol.source == "zerodha_nfo":
        return "option" if bf_symbol.option_type else "future"
    return "equity"


async def sync_symbol_to_catalog(db: AsyncSession, bf_symbol: BfSymbol) -> CatalogSyncResult:
    exchange = _SOURCE_TO_EXCHANGE.get(bf_symbol.source)
    data_source = _SOURCE_TO_DATA_SOURCE.get(bf_symbol.source)
    if exchange is None or data_source is None:
        raise CatalogSyncError(f"No instrument catalog mapping for source '{bf_symbol.source}'")

    instrument = (
        await db.execute(select(Instrument).where(Instrument.exchange == exchange, Instrument.symbol == bf_symbol.symbol))
    ).scalar_one_or_none()
    instrument_created = False
    if instrument is None:
        underlying_id = (
            await _resolve_underlying(db, bf_symbol.underlying_symbol) if bf_symbol.source == "zerodha_nfo" else None
        )
        instrument = Instrument(
            exchange=exchange,
            symbol=bf_symbol.symbol,
            name=bf_symbol.display_name,
            instrument_type=_instrument_type_for(bf_symbol),
            data_source=data_source,
            external_ref=bf_symbol.symbol,
            expiry=bf_symbol.expiry,
            strike=bf_symbol.strike,
            option_type=bf_symbol.option_type,
            lot_size=bf_symbol.lot_size,
            underlying_instrument_id=underlying_id,
        )
        db.add(instrument)
        await db.flush()
        instrument_created = True
    else:
        if not instrument.is_active:
            # This bridge only ever runs on a symbol the user explicitly
            # chose to sync from their own watchlist -- re-activate it if
            # it was previously deactivated (e.g. it predates a catalog
            # cleanup).
            instrument.is_active = True
        if instrument.data_source != data_source:
            # A seeded/pre-existing instrument with no live source backing
            # it (data_source="unassigned", or a stale legacy value like
            # "yahoo_nse" from before that source was retired) keeps that
            # label forever unless updated here -- even after Zerodha
            # starts writing real candles into it, which would otherwise
            # leave it permanently hidden from Markets/Charts (both filter
            # out anything but a real data_source app-wide, see
            # market-data pages) despite having real current data. Zerodha
            # is the live source for NSE now, so it takes over the label
            # whenever it re-syncs an existing row.
            instrument.data_source = data_source

    bars = (await db.execute(select(BfOhlcvBar).where(BfOhlcvBar.symbol_id == bf_symbol.id))).scalars().all()
    if not bars:
        bf_symbol.last_synced_at = datetime.now(timezone.utc)
        return CatalogSyncResult(
            symbol=bf_symbol.symbol, instrument_id=str(instrument.id),
            instrument_created=instrument_created, bars_synced=0, bars_skipped=0,
        )

    existing = {
        (row.timeframe, as_aware_utc(row.ts))
        for row in (
            await db.execute(select(OhlcvCandle.timeframe, OhlcvCandle.ts).where(OhlcvCandle.instrument_id == instrument.id))
        ).all()
    }

    synced = 0
    skipped = 0
    for bar in bars:
        key = (bar.timeframe, as_aware_utc(bar.ts))
        if key in existing:
            skipped += 1
            continue
        db.add(
            OhlcvCandle(
                instrument_id=instrument.id, timeframe=bar.timeframe, ts=bar.ts,
                open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume,
                open_interest=bar.open_interest, source=f"bf_{bf_symbol.source}",
            )
        )
        existing.add(key)
        synced += 1

    bf_symbol.last_synced_at = datetime.now(timezone.utc)
    return CatalogSyncResult(
        symbol=bf_symbol.symbol, instrument_id=str(instrument.id),
        instrument_created=instrument_created, bars_synced=synced, bars_skipped=skipped,
    )
