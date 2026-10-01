"""Expired NFO contracts are retired four trading days after expiry
(backfill_platform/retire_expired.py): stock contracts deleted with their
candles, index contracts hidden but keeping theirs, anything still in use
left alone, memory cleared."""

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import AuditLog
from app.models.backfill_platform import BfCoverage, BfOhlcvBar, BfSymbol, BfWatchlist, BfWatchlistItem
from app.models.fo_scan import FoOiSnapshot
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.backfill_platform import retire_expired as retire
from app.services.backfill_platform import topup
from app.services.market_data.tick_engine import tick_engine

T0 = datetime(2026, 9, 29, 3, 45, tzinfo=timezone.utc)
EXPIRED = date(2026, 9, 29)  # a Tuesday: 4 trading days on is Tue 6 Oct (2 Oct is a holiday)
RETIRED_ON = date(2026, 10, 6)


def test_the_cutoff_is_four_trading_days_back_skipping_weekends_and_holidays():
    assert retire.retire_cutoff(date(2026, 10, 6)) == date(2026, 9, 29)  # 30 Sep, 1 Oct, 5 Oct, 6 Oct
    assert retire.retire_cutoff(date(2026, 10, 5)) == date(2026, 9, 28)  # one day short for 29 Sep's contracts
    assert retire.retire_cutoff(date(2026, 10, 1)) == date(2026, 9, 25)  # across the weekend


def test_index_contracts_are_told_from_stock_contracts():
    assert retire.is_index_contract("NIFTY26SEPFUT") and retire.is_index_contract("BANKNIFTY2692925000CE")
    assert retire.is_index_contract("MIDCPNIFTY26SEPFUT") and retire.is_index_contract("X", "NIFTY")
    assert not retire.is_index_contract("RELIANCE26SEP1400CE") and not retire.is_index_contract("BANKINDIA26SEPFUT", "BANKINDIA")


async def _contract(db: AsyncSession, name: str, expiry: date, *, underlying: str, option_type: str | None, owner, nifty: Instrument) -> dict:
    sym = BfSymbol(source="zerodha_nfo", symbol=name, display_name=name, underlying_symbol=underlying, option_type=option_type, expiry=expiry)
    inst = Instrument(exchange="NFO", symbol=name, name=name, instrument_type="option" if option_type else "future", data_source="zerodha_kite",
                      external_ref=name, expiry=expiry, option_type=option_type, underlying_instrument_id=nifty.id)
    watchlist = (await db.execute(select(BfWatchlist).where(BfWatchlist.owner_id == owner.id))).scalars().first()
    db.add_all([sym, inst])
    await db.flush()
    for i in range(3):
        ts = T0 + timedelta(minutes=15 * i)
        db.add(BfOhlcvBar(symbol_id=sym.id, timeframe="15m", ts=ts, open=1, high=1, low=1, close=1))
        db.add(OhlcvCandle(instrument_id=inst.id, timeframe="15m", ts=ts, open=1, high=1, low=1, close=1, source="kite"))
    db.add(BfCoverage(symbol_id=sym.id, timeframe="15m", first_ts=T0, last_ts=T0, bar_count=3))
    db.add(BfWatchlistItem(watchlist_id=watchlist.id, symbol_id=sym.id))
    db.add(FoOiSnapshot(session_date=date(2026, 9, 28), mark="close", underlying_id=nifty.id, instrument_id=inst.id,
                        kind=option_type or "FUT", expiry=expiry, oi=1.0, captured_at=T0))
    return {"sym": sym.id, "inst": inst.id}


async def _count(db: AsyncSession, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return (await db.execute(stmt)).scalar_one()


async def _world(db: AsyncSession) -> dict[str, dict]:
    user = User(email=f"retire_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="Retire")
    db.add(user)
    await db.flush()
    db.add(BfWatchlist(owner_id=user.id, name="Contracts"))
    nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db.add(nifty)
    await db.flush()
    make = lambda name, expiry, underlying, option_type: _contract(db, name, expiry, underlying=underlying, option_type=option_type, owner=user, nifty=nifty)  # noqa: E731
    world = {
        "stock_option": await make("RELIANCE26SEP1400CE", EXPIRED, "RELIANCE", "CE"),
        "stock_future": await make("RELIANCE26SEPFUT", EXPIRED, "RELIANCE", None),
        "index_option": await make("NIFTY26SEP25000CE", EXPIRED, "NIFTY", "CE"),
        "index_future": await make("NIFTY26SEPFUT", EXPIRED, "NIFTY", None),
        "too_recent": await make("RELIANCE26OCT1400CE", date(2026, 9, 30), "RELIANCE", "CE"),
        "live": await make("RELIANCE26NOVFUT", date(2026, 11, 24), "RELIANCE", None),
        "in_use": await make("TCS26SEP3000CE", EXPIRED, "TCS", "CE"),
    }
    # A native strategy still holds the "in use" contract in its saved state.
    strategy = Strategy(name="Holds an expired contract", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={}, python_code="#",
                              position_sizing={}, risk_rules={})
    portfolio = PaperPortfolio(user_id=user.id, name="P", cash=1.0, initial_capital=1.0)
    db.add_all([version, portfolio])
    await db.flush()
    db.add(PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, status=DeploymentStatus.STOPPED.value,
        state={"position": {"legs": {"short_ce": {"instrument_id": str(world["in_use"]["inst"]), "side": "sell"}}}},
    ))
    await db.commit()
    return world


async def test_stock_contracts_are_deleted_index_contracts_hidden_and_what_is_in_use_kept(db_engine, db_session: AsyncSession, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(retire, "PAUSE_SECONDS", 0)
    world = await _world(db_session)
    for name in ("stock_option", "index_option"):
        tick_engine.set_real_price(world[name]["inst"], 101.0, source="test")

    result = await retire.retire_expired_contracts(RETIRED_ON, factory)
    db_session.expire_all()

    for name in ("stock_option", "stock_future"):  # gone from everywhere
        ids = world[name]
        assert await _count(db_session, BfSymbol, id=ids["sym"]) == 0 and await _count(db_session, Instrument, id=ids["inst"]) == 0
        assert await _count(db_session, BfOhlcvBar, symbol_id=ids["sym"]) == 0 and await _count(db_session, OhlcvCandle, instrument_id=ids["inst"]) == 0
        assert await _count(db_session, BfCoverage, symbol_id=ids["sym"]) == 0 and await _count(db_session, BfWatchlistItem, symbol_id=ids["sym"]) == 0
        assert await _count(db_session, FoOiSnapshot, instrument_id=ids["inst"]) == 0

    for name in ("index_option", "index_future"):  # hidden, off the watchlist, candles kept
        ids = world[name]
        instrument = await db_session.get(Instrument, ids["inst"])
        assert instrument is not None and instrument.is_active is False
        assert await _count(db_session, BfSymbol, id=ids["sym"]) == 1 and await _count(db_session, BfOhlcvBar, symbol_id=ids["sym"]) == 3
        assert await _count(db_session, OhlcvCandle, instrument_id=ids["inst"]) == 3 and await _count(db_session, BfCoverage, symbol_id=ids["sym"]) == 1
        assert await _count(db_session, BfWatchlistItem, symbol_id=ids["sym"]) == 0

    for name in ("too_recent", "live", "in_use"):  # untouched
        ids = world[name]
        instrument = await db_session.get(Instrument, ids["inst"])
        assert instrument is not None and instrument.is_active is True
        assert await _count(db_session, BfSymbol, id=ids["sym"]) == 1 and await _count(db_session, BfOhlcvBar, symbol_id=ids["sym"]) == 3
        assert await _count(db_session, OhlcvCandle, instrument_id=ids["inst"]) == 3 and await _count(db_session, BfWatchlistItem, symbol_id=ids["sym"]) == 1

    assert (result.stock_symbols_deleted, result.backfill_bars_deleted, result.stock_instruments_deleted) == (2, 6, 2)
    assert (result.chart_candles_deleted, result.oi_snapshots_deleted, result.index_instruments_hidden) == (6, 2, 2)
    assert (result.watchlist_items_removed, result.kept_in_use) == (4, 1)
    assert tick_engine.get_current_price(world["stock_option"]["inst"]) is None  # memory cleared
    assert tick_engine.get_current_price(world["index_option"]["inst"]) is None

    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "NFO_EXPIRED_RETIRED"))).scalars().all()
    assert len(audit) == 1 and audit[0].new_value["stock_symbols_deleted"] == 2 and audit[0].new_value["cutoff"] == "2026-09-29"

    again = await retire.retire_expired_contracts(RETIRED_ON, factory)  # nothing left to do, nothing recorded
    assert not again.changed
    assert len((await db_session.execute(select(AuditLog).where(AuditLog.action == "NFO_EXPIRED_RETIRED"))).scalars().all()) == 1


async def test_a_contract_one_trading_day_short_is_left_alone(db_engine, db_session: AsyncSession, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(retire, "PAUSE_SECONDS", 0)
    world = await _world(db_session)

    result = await retire.retire_expired_contracts(date(2026, 10, 5), factory)  # 29 Sep + 3 trading days
    assert not result.changed
    assert await _count(db_session, BfSymbol) == len(world) and await _count(db_session, Instrument, is_active=True) == len(world) + 1  # + NIFTY 50
    assert await _count(db_session, BfWatchlistItem) == len(world)


async def test_the_nightly_backfill_starts_the_retirement_once_per_session(db_engine, db_session: AsyncSession, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(topup, "AsyncSessionLocal", factory)
    monkeypatch.setattr(retire, "PAUSE_SECONDS", 0)
    world = await _world(db_session)

    scheduler = topup.BackfillTopupScheduler()
    scheduler._start_retire(RETIRED_ON)
    scheduler._start_retire(RETIRED_ON)  # the same session again: not started twice
    await scheduler._retire_task

    assert await _count(db_session, BfSymbol, id=world["stock_option"]["sym"]) == 0
    assert (await db_session.get(Instrument, world["index_future"]["inst"])).is_active is False


async def test_syncing_to_the_catalog_does_not_bring_back_an_expired_contract(db_session: AsyncSession):
    from app.services.backfill_platform.catalog_sync import sync_symbol_to_catalog

    for name, expiry, expect_active in (("OLD26JANFUT", date(2020, 1, 30), False), ("NEW99JANFUT", date(2099, 1, 30), True)):
        sym = BfSymbol(source="zerodha_nfo", symbol=name, display_name=name, expiry=expiry)
        db_session.add_all([
            sym, Instrument(exchange="NFO", symbol=name, name=name, instrument_type="future", data_source="zerodha_kite", external_ref=name,
                            expiry=expiry, is_active=False),
        ])
        await db_session.flush()
        await sync_symbol_to_catalog(db_session, sym)
        instrument = (await db_session.execute(select(Instrument).where(Instrument.symbol == name))).scalar_one()
        assert instrument.is_active is expect_active, name


async def test_a_contract_a_live_strategy_holds_is_kept(db_engine, db_session: AsyncSession, monkeypatch):
    from app.models.broker import Broker, BrokerAccount
    from app.models.live_native import LiveNativeDeployment, LiveNativePosition

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(retire, "PAUSE_SECONDS", 0)
    world = await _world(db_session)
    stock_option = await db_session.get(Instrument, world["stock_option"]["inst"])
    strategy = (await db_session.execute(select(Strategy))).scalars().first()
    version = (await db_session.execute(select(StrategyVersion))).scalars().first()
    broker = Broker(code="zerodha_kite_retire_test", name="Kite", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    account = BrokerAccount(user_id=strategy.owner_id, broker_id=broker.id, account_label="Kite", environment="live")
    db_session.add(account)
    await db_session.flush()
    deployment = LiveNativeDeployment(owner_id=strategy.owner_id, strategy_id=strategy.id, strategy_version_id=version.id,
                                      broker_account_id=account.id, status="active", capital=100000.0)
    db_session.add(deployment)
    await db_session.flush()
    db_session.add(LiveNativePosition(deployment_id=deployment.id, instrument_id=stock_option.id, quantity=-65.0, avg_price=10.0,
                                      strategy_quantity=650.0, opened_at=T0))
    await db_session.commit()

    result = await retire.retire_expired_contracts(RETIRED_ON, factory)
    assert await _count(db_session, Instrument, id=world["stock_option"]["inst"]) == 1  # held live: kept
    assert await _count(db_session, Instrument, id=world["stock_future"]["inst"]) == 0  # not held: gone
    assert result.kept_in_use == 2  # with the paper strategy's "in use" one
