"""Migration f5a6b7c8d9e0: NSE renamed HEG to HEGAM. The backfill symbol and
the chart instrument are renamed in place -- history, nightly coverage and
watchlists carry over -- or, if HEGAM is already there, HEG's watchlist
entries move to it and HEG leaves the nightly top-up."""

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.backfill_platform import BfCoverage, BfOhlcvBar, BfSymbol, BfWatchlist, BfWatchlistItem
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.user import User

MIGRATION = pathlib.Path(__file__).parent.parent / "alembic/versions/f5a6b7c8d9e0_rename_heg_to_hegam.py"
LAST = datetime(2026, 9, 15, 9, 45, tzinfo=timezone.utc)


async def _run_migration(db_engine) -> None:
    spec = importlib.util.spec_from_file_location("rename_heg", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    async with db_engine.begin() as conn:
        await conn.run_sync(run)


async def _stock(db, symbol: str) -> tuple[BfSymbol, Instrument]:
    bf = BfSymbol(source="zerodha", symbol=symbol, display_name=f"{symbol} LTD")
    inst = Instrument(exchange="NSE", symbol=symbol, name=f"{symbol} LTD", instrument_type="equity", data_source="zerodha_kite", external_ref=symbol)
    db.add_all([bf, inst])
    await db.flush()
    db.add_all([
        BfOhlcvBar(symbol_id=bf.id, timeframe="1d", ts=LAST, open=239.0, high=241.0, low=236.0, close=237.3, volume=1000.0),
        BfCoverage(symbol_id=bf.id, timeframe="1d", first_ts=LAST - timedelta(days=30), last_ts=LAST, bar_count=20, last_day_bars=1),
        OhlcvCandle(instrument_id=inst.id, timeframe="1d", ts=LAST, open=239.0, high=241.0, low=236.0, close=237.3, volume=1000.0, source="test"),
    ])
    return bf, inst


async def _watchlist(db, *symbols: BfSymbol) -> BfWatchlist:
    user = User(email=f"heg_{len(symbols)}@tradingmaster.internal", hashed_password="x", full_name="HEG")
    db.add(user)
    await db.flush()
    wl = BfWatchlist(owner_id=user.id, name="NSE 500")
    db.add(wl)
    await db.flush()
    db.add_all([BfWatchlistItem(watchlist_id=wl.id, symbol_id=s.id) for s in symbols])
    return wl


async def test_heg_is_renamed_in_place_keeping_its_history_coverage_and_watchlist(db_engine, db_session):
    bf, inst = await _stock(db_session, "HEG")
    wl = await _watchlist(db_session, bf)
    await db_session.commit()
    bf_id, inst_id, wl_id = bf.id, inst.id, wl.id

    await _run_migration(db_engine)
    await _run_migration(db_engine)  # a second run changes nothing
    db_session.expire_all()

    renamed = await db_session.get(BfSymbol, bf_id)
    assert (renamed.symbol, renamed.display_name) == ("HEGAM", "HEG ADVANCED MATERIAL")
    assert (await db_session.execute(select(BfOhlcvBar).where(BfOhlcvBar.symbol_id == bf_id))).scalar_one().close == 237.3
    assert (await db_session.execute(select(BfCoverage).where(BfCoverage.symbol_id == bf_id))).scalar_one().last_ts is not None
    item = (await db_session.execute(select(BfWatchlistItem).where(BfWatchlistItem.watchlist_id == wl_id))).scalar_one()
    assert item.symbol_id == bf_id

    chart = await db_session.get(Instrument, inst_id)
    assert (chart.symbol, chart.external_ref, chart.is_active) == ("HEGAM", "HEGAM", True)
    assert (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == inst_id))).scalar_one().close == 237.3

    actions = (await db_session.execute(select(AuditLog.action).order_by(AuditLog.action))).scalars().all()
    assert actions == ["BF_SYMBOL_RENAMED", "INSTRUMENT_RENAMED"]


async def test_when_hegam_is_already_there_heg_hands_over_and_leaves_the_nightly_top_up(db_engine, db_session):
    heg, heg_inst = await _stock(db_session, "HEG")
    hegam, hegam_inst = await _stock(db_session, "HEGAM")
    both = await _watchlist(db_session, heg, hegam)  # already holds both: HEG's entry just goes
    user = (await db_session.execute(select(User).where(User.email == "heg_2@tradingmaster.internal"))).scalar_one()
    only_old = BfWatchlist(owner_id=user.id, name="Graphite")
    db_session.add(only_old)
    await db_session.flush()
    db_session.add(BfWatchlistItem(watchlist_id=only_old.id, symbol_id=heg.id))
    await db_session.commit()
    ids = {"heg": heg.id, "hegam": hegam.id, "both": both.id, "only_old": only_old.id, "heg_inst": heg_inst.id, "hegam_inst": hegam_inst.id}

    await _run_migration(db_engine)
    await _run_migration(db_engine)
    db_session.expire_all()

    async def members(watchlist_id):
        return set((await db_session.execute(select(BfWatchlistItem.symbol_id).where(BfWatchlistItem.watchlist_id == watchlist_id))).scalars())

    assert await members(ids["both"]) == {ids["hegam"]}
    assert await members(ids["only_old"]) == {ids["hegam"]}
    assert (await db_session.execute(select(BfCoverage).where(BfCoverage.symbol_id == ids["heg"]))).first() is None
    assert (await db_session.execute(select(BfCoverage).where(BfCoverage.symbol_id == ids["hegam"]))).first() is not None
    assert (await db_session.get(BfSymbol, ids["heg"])).symbol == "HEG"  # its history stays, under its old name
    assert (await db_session.get(Instrument, ids["heg_inst"])).is_active is False
    assert (await db_session.get(Instrument, ids["hegam_inst"])).is_active is True

    actions = (await db_session.execute(select(AuditLog.action).order_by(AuditLog.action))).scalars().all()
    assert actions == ["BF_SYMBOL_REPLACED", "INSTRUMENT_HIDDEN"]
