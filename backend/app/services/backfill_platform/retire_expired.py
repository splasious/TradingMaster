"""Retires NFO contracts four trading days after they expire.

Approved 1 Oct 2026. An expired contract is useless to the live app -- Kite
lists nothing for it, nothing trades it -- yet each one kept its backfill
symbol, chart-catalog row (flagged active, so still in lists and search),
watchlist entry, saved candles and OI-scan snapshots for good: after the 29
Sep expiry that was 12,490 contracts, a quarter of the backfill bars.

Once a contract has been expired for RETIRE_AFTER_TRADING_DAYS trading days
(weekends and NSE holidays don't count), at the nightly backfill:
  - stock options and stock futures are deleted: backfill symbol with its
    bars, coverage, jobs and watchlist entries; chart-catalog row with its
    candles, backfill jobs and OI snapshots;
  - index contracts (NIFTY, BANKNIFTY, ...) keep their candles -- they are
    what backtests of past weeklies run on -- but leave every watchlist and
    are flagged inactive, so lists and search no longer show them;
  - their prices leave the live feed's memory.
A contract something still refers to -- a deployment, backtest or order, a
live strategy's holding, or a native strategy's saved state (paper or live)
-- is left exactly as it is. Trade history
keeps each leg's own symbol, strike and expiry, so it reads the same.

Runs in the background, a few contracts per statement with pauses between
(as purge.py), so the app stays responsive while ~1M bars go; carrying on
after a restart is automatic: whatever is left is still past the limit.
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date

from sqlalchemy import delete, select, update

from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfBackfillJob, BfCoverage, BfOhlcvBar, BfSymbol, BfWatchlistItem
from app.models.backtest import BacktestJob, OptimizationJob, PortfolioBacktestTrade
from app.models.fo_scan import FoOiSnapshot
from app.models.instrument import Instrument
from app.models.live_native import LiveNativeDeployment, LiveNativePosition
from app.models.live_trading import LiveDeployment, LiveOrder
from app.models.market_data import BackfillJob, OhlcvCandle
from app.models.paper_trading import PaperDeployment, PaperNativeDeployment
from app.services.audit import write_audit_log
from app.services.backfill_platform.coverage import previous_trading_day
from app.services.backfill_platform.topup import INDEX_UNDERLYINGS
from app.services.market_data.active_timeframe_sync_scheduler import active_timeframe_sync_scheduler, forget_native_demand
from app.services.market_data.tick_engine import tick_engine

logger = logging.getLogger(__name__)

RETIRE_AFTER_TRADING_DAYS = 4
# Contracts per statement, and the pause between them, so the deletes never
# crowd out the app's own queries.
CONTRACT_BATCH = 20
PAUSE_SECONDS = 0.05

# An index contract's tradingsymbol: NIFTY26OCTFUT, BANKNIFTY2610622700CE ...
_INDEX_TRADINGSYMBOL = re.compile(r"^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTYNXT50|NIFTY)\d")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def retire_cutoff(session: date) -> date:
    """The latest expiry retired at `session`'s backfill: the trading day
    RETIRE_AFTER_TRADING_DAYS before it. 29 Sep's contracts are retired on
    6 Oct -- 30 Sep, 1 Oct, 5 Oct, 6 Oct, with 2 Oct a holiday."""
    day = session
    for _ in range(RETIRE_AFTER_TRADING_DAYS):
        day = previous_trading_day(day)
    return day


def is_index_contract(tradingsymbol: str, underlying_name: str | None = None) -> bool:
    return underlying_name in INDEX_UNDERLYINGS or bool(_INDEX_TRADINGSYMBOL.match(tradingsymbol))


@dataclass
class RetireResult:
    cutoff: date
    stock_symbols_deleted: int = 0
    backfill_bars_deleted: int = 0
    stock_instruments_deleted: int = 0
    chart_candles_deleted: int = 0
    oi_snapshots_deleted: int = 0
    index_instruments_hidden: int = 0
    watchlist_items_removed: int = 0
    kept_in_use: int = 0

    @property
    def changed(self) -> bool:
        return any(v for k, v in asdict(self).items() if k not in ("cutoff", "kept_in_use"))


async def _in_use(db) -> set[str]:
    """Ids of instruments that something still refers to, as strings."""
    ids: set[str] = set()
    for column in (
        PaperDeployment.instrument_id, LiveDeployment.instrument_id, LiveOrder.instrument_id, BacktestJob.instrument_id,
        OptimizationJob.instrument_id, PortfolioBacktestTrade.instrument_id, LiveNativePosition.instrument_id,
    ):
        ids.update(str(v) for v in (await db.execute(select(column).where(column.is_not(None)).distinct())).scalars())
    for model in (PaperNativeDeployment, LiveNativeDeployment):
        for state in (await db.execute(select(model.state))).scalars():
            ids.update(_UUID.findall(json.dumps(state or {})))
    return ids


def _batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def retire_expired_contracts(session: date, session_factory: Callable = AsyncSessionLocal) -> RetireResult:
    """Retires every NFO contract expired on or before retire_cutoff(session).
    Safe to run again at any time: it finds nothing more to do."""
    cutoff = retire_cutoff(session)
    result = RetireResult(cutoff=cutoff)
    forgotten: set[uuid.UUID] = set()

    # --- Chart catalog (instruments) -------------------------------------
    async with session_factory() as db:
        in_use = await _in_use(db)
        rows = (
            await db.execute(
                select(Instrument.id, Instrument.symbol, Instrument.is_active).where(
                    Instrument.exchange == "NFO", Instrument.expiry.is_not(None), Instrument.expiry <= cutoff,
                )
            )
        ).all()
    stock_ids, index_ids, kept_symbols = [], [], set()
    for instrument_id, symbol, is_active in rows:
        if str(instrument_id) in in_use:
            kept_symbols.add(symbol)
        elif is_index_contract(symbol):
            if is_active:
                index_ids.append(instrument_id)
        else:
            stock_ids.append(instrument_id)

    for ids in _batches(index_ids, 500):
        async with session_factory() as db:
            await db.execute(update(Instrument).where(Instrument.id.in_(ids)).values(is_active=False))
            await db.commit()
        result.index_instruments_hidden += len(ids)
        forgotten.update(ids)

    for ids in _batches(stock_ids, CONTRACT_BATCH):
        async with session_factory() as db:
            candles = await db.execute(delete(OhlcvCandle).where(OhlcvCandle.instrument_id.in_(ids)))
            snapshots = await db.execute(delete(FoOiSnapshot).where(FoOiSnapshot.instrument_id.in_(ids)))
            await db.execute(delete(BackfillJob).where(BackfillJob.instrument_id.in_(ids)))
            await db.execute(delete(Instrument).where(Instrument.id.in_(ids)))
            await db.commit()
        result.chart_candles_deleted += candles.rowcount or 0
        result.oi_snapshots_deleted += snapshots.rowcount or 0
        result.stock_instruments_deleted += len(ids)
        forgotten.update(ids)
        await asyncio.sleep(PAUSE_SECONDS)

    # --- Backfill symbols (bf_symbols) ------------------------------------
    async with session_factory() as db:
        symbols = (
            await db.execute(
                select(BfSymbol.id, BfSymbol.symbol, BfSymbol.underlying_symbol).where(
                    BfSymbol.source == "zerodha_nfo", BfSymbol.expiry.is_not(None), BfSymbol.expiry <= cutoff,
                )
            )
        ).all()
    stock_symbol_ids, index_symbol_ids = [], []
    for symbol_id, symbol, underlying in symbols:
        if symbol in kept_symbols:
            continue
        (index_symbol_ids if is_index_contract(symbol, underlying) else stock_symbol_ids).append(symbol_id)

    for ids in _batches(index_symbol_ids, 500):
        async with session_factory() as db:
            removed = await db.execute(delete(BfWatchlistItem).where(BfWatchlistItem.symbol_id.in_(ids)))
            await db.commit()
        result.watchlist_items_removed += removed.rowcount or 0

    for ids in _batches(stock_symbol_ids, CONTRACT_BATCH):
        async with session_factory() as db:
            bars = await db.execute(delete(BfOhlcvBar).where(BfOhlcvBar.symbol_id.in_(ids)))
            removed = await db.execute(delete(BfWatchlistItem).where(BfWatchlistItem.symbol_id.in_(ids)))
            await db.execute(delete(BfCoverage).where(BfCoverage.symbol_id.in_(ids)))
            await db.execute(delete(BfBackfillJob).where(BfBackfillJob.symbol_id.in_(ids)))
            await db.execute(delete(BfSymbol).where(BfSymbol.id.in_(ids)))
            await db.commit()
        result.backfill_bars_deleted += bars.rowcount or 0
        result.watchlist_items_removed += removed.rowcount or 0
        result.stock_symbols_deleted += len(ids)
        await asyncio.sleep(PAUSE_SECONDS)

    result.kept_in_use = len(kept_symbols)

    # --- Memory and the record --------------------------------------------
    tick_engine.forget(forgotten)
    active_timeframe_sync_scheduler.forget(forgotten)
    forget_native_demand(forgotten)
    if result.changed:
        async with session_factory() as db:
            await write_audit_log(
                db, user_id=None, action="NFO_EXPIRED_RETIRED", object_type="bf_source", object_id="zerodha_nfo",
                new_value={**asdict(result), "cutoff": cutoff.isoformat(), "session": session.isoformat()},
            )
            await db.commit()
        logger.info("Retired NFO contracts expired by %s: %s", cutoff, asdict(result))
    return result
