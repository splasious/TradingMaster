"""Bridges the Data Backfill Platform's isolated bf_* schema into the main
Instrument/OhlcvCandle schema that Charts, Strategy Builder, Backtesting,
and Optimization actually read from. The two schemas are deliberately kept
separate while backfilling (per that module's own "no cross-source
merging" rule) -- this is the bridge between them, run either on demand
(the "Sync" buttons) or continuously by CatalogSyncScheduler. Never
overwrites a candle the main catalog already has; a bar already present
for (instrument, timeframe, ts) is left alone."""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle

_SOURCE_TO_EXCHANGE = {"yahoo": "NSE", "delta": "DELTA"}
_SOURCE_TO_DATA_SOURCE = {"yahoo": "yahoo_nse", "delta": "delta_exchange"}


class CatalogSyncError(Exception):
    """Raised when a bf_symbol's source has no main-catalog mapping (e.g.
    Zerodha isn't synced into the main Instrument catalog today)."""


@dataclass
class CatalogSyncResult:
    symbol: str
    instrument_id: str
    instrument_created: bool
    bars_synced: int
    bars_skipped: int


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
        instrument = Instrument(
            exchange=exchange,
            symbol=bf_symbol.symbol,
            name=bf_symbol.display_name,
            instrument_type="perpetual_future" if bf_symbol.source == "delta" else "equity",
            data_source=data_source,
            external_ref=bf_symbol.symbol,
        )
        db.add(instrument)
        await db.flush()
        instrument_created = True
    elif not instrument.is_active:
        # This bridge only ever runs on a symbol the user explicitly chose
        # to sync from their own watchlist -- re-activate it if it was
        # previously deactivated (e.g. it predates a catalog cleanup).
        instrument.is_active = True

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
                source=f"bf_{bf_symbol.source}",
            )
        )
        existing.add(key)
        synced += 1

    bf_symbol.last_synced_at = datetime.now(timezone.utc)
    return CatalogSyncResult(
        symbol=bf_symbol.symbol, instrument_id=str(instrument.id),
        instrument_created=instrument_created, bars_synced=synced, bars_skipped=skipped,
    )
