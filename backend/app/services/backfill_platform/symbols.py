"""Symbol search scoped to one source (PRD 4.1: "Symbol/instrument search
with autocomplete scoped to that exchange"), plus get-or-create for the
local bf_symbols row a backfill job or watchlist item points at."""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfSymbol, BfWatchlistItem
from app.services.backfill_platform.kite_auth import get_authenticated_kite_broker
from app.services.broker.zerodha_broker import KiteAPIError
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource


@dataclass
class SymbolSearchResult:
    symbol: str
    display_name: str
    # F&O metadata (source="zerodha_nfo" only; always None for delta/zerodha) --
    # carried through get_or_create_symbol into BfSymbol, see that model's
    # docstring for why it's captured here rather than re-fetched at sync time.
    expiry: date | None = None
    strike: float | None = None
    option_type: str | None = None
    lot_size: int | None = None
    underlying_symbol: str | None = None


def _parse_nfo_row(row: dict) -> dict:
    kite_type = (row.get("instrument_type") or "").upper()
    expiry_str = row.get("expiry")
    expiry = date.fromisoformat(expiry_str) if expiry_str else None
    strike_raw = row.get("strike")
    strike = float(strike_raw) if strike_raw not in (None, "", "0") else None
    lot_size_raw = row.get("lot_size")
    return {
        "option_type": kite_type if kite_type in ("CE", "PE") else None,
        "expiry": expiry,
        "strike": strike if kite_type in ("CE", "PE") else None,
        "lot_size": int(lot_size_raw) if lot_size_raw else None,
        "underlying_symbol": row.get("name") or None,
    }


async def search_symbols(db: AsyncSession, source: str, query: str, user_id) -> list[SymbolSearchResult]:
    query_upper = query.upper().strip()

    if source == "delta":
        try:
            # RWA/tokenized-US-equity products only -- crypto is deliberately
            # excluded from the Data Backfill Platform's Delta block.
            products = await DeltaExchangeDataSource().list_rwa_token_products()
        except MarketDataSourceError as exc:
            raise MarketDataSourceError(str(exc)) from exc
        matches = [p for p in products if query_upper in p["symbol"].upper() or query_upper in (p.get("description") or "").upper()]
        return [SymbolSearchResult(symbol=p["symbol"], display_name=p.get("description") or p["symbol"]) for p in matches[:50]]

    if source == "zerodha":
        try:
            broker = await get_authenticated_kite_broker(db, user_id)
            instruments = await broker.get_instruments()
        except KiteAPIError as exc:
            raise MarketDataSourceError(str(exc)) from exc
        matches = [
            i for i in instruments
            if query_upper in (i.get("tradingsymbol") or "").upper() or query_upper in (i.get("name") or "").upper()
        ]
        return [SymbolSearchResult(symbol=i["tradingsymbol"], display_name=i.get("name") or i["tradingsymbol"]) for i in matches[:50]]

    if source == "zerodha_nfo":
        try:
            broker = await get_authenticated_kite_broker(db, user_id)
            instruments = await broker.get_instruments("NFO")
        except KiteAPIError as exc:
            raise MarketDataSourceError(str(exc)) from exc
        matches = [
            i for i in instruments
            if query_upper in (i.get("tradingsymbol") or "").upper() or query_upper in (i.get("name") or "").upper()
        ]
        return [
            SymbolSearchResult(symbol=i["tradingsymbol"], display_name=i.get("tradingsymbol", ""), **_parse_nfo_row(i))
            for i in matches[:50]
        ]

    raise MarketDataSourceError(f"Unknown source '{source}'")


async def list_all_symbols(db: AsyncSession, source: str, user_id) -> list[SymbolSearchResult]:
    """The full tracked universe for a source, unfiltered by a query --
    used by "Backfill All" (PRD 4's watchlist bulk actions, extended to a
    whole source).

    For Delta that universe is a real, naturally bounded exchange catalog:
    its RWA token list. Zerodha has no such bounded catalog of its own --
    Kite's NSE instrument dump is the *entire* exchange (equities, indices,
    ETFs, bonds, preference shares -- 10,000+ rows), not something any user
    actually wants backfilled wholesale. So for Zerodha "all tracked
    symbols" means exactly that: symbols this user has explicitly added to
    a watchlist, the same set the button's own label promises -- never a
    live Kite API call, and never more than what was deliberately opted
    into."""
    if source == "delta":
        products = await DeltaExchangeDataSource().list_rwa_token_products()
        return [SymbolSearchResult(symbol=p["symbol"], display_name=p.get("description") or p["symbol"]) for p in products]
    if source in ("zerodha", "zerodha_nfo"):
        watched = (
            await db.execute(
                select(BfSymbol.symbol, BfSymbol.display_name)
                .join(BfWatchlistItem, BfWatchlistItem.symbol_id == BfSymbol.id)
                .where(BfSymbol.source == source)
                .distinct()
            )
        ).all()
        return [SymbolSearchResult(symbol=s, display_name=d) for s, d in watched]
    raise MarketDataSourceError(f"Unknown source '{source}'")


async def get_or_create_symbol(
    db: AsyncSession, source: str, symbol: str, display_name: str,
    expiry: date | None = None, strike: float | None = None, option_type: str | None = None,
    lot_size: int | None = None, underlying_symbol: str | None = None,
) -> BfSymbol:
    """Concurrent callers race here more than it looks -- selecting several
    timeframes for one symbol fires one request per timeframe in parallel
    (both the single-symbol and "backfill all" flows), and every one of
    them resolves the same brand-new (source, symbol) pair at once. A plain
    select-then-insert lets two of them both see "not found" and both try
    to INSERT, so the loser crashes the whole request on the unique
    constraint. ON CONFLICT DO NOTHING makes the insert itself race-safe;
    the follow-up select then always finds a row, ours or the winner's."""
    existing = (
        await db.execute(select(BfSymbol).where(BfSymbol.source == source, BfSymbol.symbol == symbol))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    await db.execute(
        pg_insert(BfSymbol)
        .values(
            source=source, symbol=symbol, display_name=display_name,
            expiry=expiry, strike=strike, option_type=option_type,
            lot_size=lot_size, underlying_symbol=underlying_symbol,
        )
        .on_conflict_do_nothing(index_elements=["source", "symbol"])
    )
    return (
        await db.execute(select(BfSymbol).where(BfSymbol.source == source, BfSymbol.symbol == symbol))
    ).scalar_one()
