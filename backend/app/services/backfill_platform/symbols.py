"""Symbol search scoped to one source (PRD 4.1: "Symbol/instrument search
with autocomplete scoped to that exchange"), plus get-or-create for the
local bf_symbols row a backfill job or watchlist item points at."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfSymbol
from app.services.backfill_platform.kite_auth import get_authenticated_kite_broker
from app.services.broker.zerodha_broker import KiteAPIError
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.market_data.yahoo_source import YahooNSEDataSource


@dataclass
class SymbolSearchResult:
    symbol: str
    display_name: str


async def search_symbols(db: AsyncSession, source: str, query: str, user_id) -> list[SymbolSearchResult]:
    query_upper = query.upper().strip()

    if source == "yahoo":
        try:
            symbols = await YahooNSEDataSource().list_symbols()
        except MarketDataSourceError as exc:
            raise MarketDataSourceError(str(exc)) from exc
        matches = [s for s in symbols if query_upper in s["nse_code"].upper() or query_upper in (s.get("name") or "").upper()]
        return [SymbolSearchResult(symbol=s["nse_code"], display_name=s.get("name") or s["nse_code"]) for s in matches[:50]]

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

    raise MarketDataSourceError(f"Unknown source '{source}'")


async def list_all_symbols(db: AsyncSession, source: str, user_id) -> list[SymbolSearchResult]:
    """The full tracked universe for a source, unfiltered by a query --
    used by "Backfill All" (PRD 4's watchlist bulk actions, extended to a
    whole source): nse-yahoo-data's ~750 NSE symbols, or Delta's RWA
    token list."""
    if source == "yahoo":
        symbols = await YahooNSEDataSource().list_symbols()
        return [SymbolSearchResult(symbol=s["nse_code"], display_name=s.get("name") or s["nse_code"]) for s in symbols if s.get("is_active", True)]
    if source == "delta":
        products = await DeltaExchangeDataSource().list_rwa_token_products()
        return [SymbolSearchResult(symbol=p["symbol"], display_name=p.get("description") or p["symbol"]) for p in products]
    if source == "zerodha":
        try:
            broker = await get_authenticated_kite_broker(db, user_id)
            instruments = await broker.get_instruments()
        except KiteAPIError as exc:
            raise MarketDataSourceError(str(exc)) from exc
        return [SymbolSearchResult(symbol=i["tradingsymbol"], display_name=i.get("name") or i["tradingsymbol"]) for i in instruments]
    raise MarketDataSourceError(f"Unknown source '{source}'")


async def get_or_create_symbol(db: AsyncSession, source: str, symbol: str, display_name: str) -> BfSymbol:
    existing = (
        await db.execute(select(BfSymbol).where(BfSymbol.source == source, BfSymbol.symbol == symbol))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = BfSymbol(source=source, symbol=symbol, display_name=display_name)
    db.add(row)
    await db.flush()
    return row
