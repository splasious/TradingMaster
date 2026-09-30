"""Migrations of 30 Sep evening: a6b7c8d9e0f1 marks for one more copy the NFO
contracts whose live-feed candles still differ from Kite's final ones, and
4abb8d69a900 moves MACD - RSI - 15 MIN's running deployment onto the fresh
up-cross version, keeping its holdings."""

import hashlib
import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User

VERSIONS = pathlib.Path(__file__).parent.parent / "alembic/versions"
BUILTIN = pathlib.Path(__file__).parent.parent / "app/services/strategy/native_strategies/macd_rsi_15min.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(db_engine, migration) -> None:
    def run(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    async with db_engine.begin() as conn:
        await conn.run_sync(run)


# ---------------------------------------------------------------- re-copy --

async def _contract(db, symbol: str, ts: datetime, live_close: float, final_close: float) -> BfSymbol:
    copied = datetime(2026, 9, 29, 14, 20, tzinfo=timezone.utc)
    bf = BfSymbol(source="zerodha_nfo", symbol=symbol, display_name=symbol, option_type="CE", underlying_symbol="NIFTY", last_synced_at=copied)
    inst = Instrument(exchange="NFO", symbol=symbol, name=symbol, instrument_type="option", data_source="zerodha_kite", external_ref=symbol)
    db.add_all([bf, inst])
    await db.flush()
    db.add_all([
        BfOhlcvBar(symbol_id=bf.id, timeframe="15m", ts=ts, open=100.0, high=110.0, low=95.0, close=final_close, volume=5000.0),
        OhlcvCandle(instrument_id=inst.id, timeframe="15m", ts=ts, open=100.0, high=104.0, low=99.0, close=live_close, volume=None, source="kite_live"),
    ])
    return bf


async def test_contracts_with_differing_live_candles_are_copied_again(db_engine, db_session):
    recent = datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(days=6)
    differs = await _contract(db_session, "NIFTY26929A", recent, live_close=102.0, final_close=108.0)
    sampled_no_volume = await _contract(db_session, "NIFTY26929B", recent, live_close=108.0, final_close=108.0)
    too_old = await _contract(db_session, "NIFTY26929C", recent - timedelta(days=30), live_close=102.0, final_close=108.0)
    await db_session.commit()
    ids = (differs.id, sampled_no_volume.id, too_old.id)

    await _run(db_engine, _load("a6b7c8d9e0f1_recopy_contracts_with_live_candles"))
    db_session.expire_all()

    synced = [(await db_session.get(BfSymbol, i)).last_synced_at for i in ids]
    assert synced[0] is None and synced[1] is None  # a price or a missing volume: copied again
    assert synced[2] is not None  # past the 21 days the copy replaces: left alone


# ------------------------------------------------------------ MACD version --

async def _deployment(db, email: str, code: str, status: str = DeploymentStatus.ACTIVE.value) -> PaperNativeDeployment:
    user = User(email=email, hashed_password="x", full_name="MACD")
    db.add(user)
    await db.flush()
    strategy = Strategy(name="MACD - RSI - 15 MIN", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=11, timeframe="15m", instrument_ids=[], parameters={"x": 1},
                              python_code=code, position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={}, created_by=user.id)
    portfolio = PaperPortfolio(user_id=user.id, name="MACD", cash=100000.0, initial_capital=100000.0)
    db.add_all([version, portfolio])
    await db.flush()
    holdings = {"SBIN": {"instrument_id": "x", "quantity": 10.0, "entry_price": 800.0, "opened_at": "2026-09-29T05:45:50+00:00"}}
    deployment = PaperNativeDeployment(portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                       status=status, state={"seeded": True, "holdings": holdings})
    db.add(deployment)
    await db.flush()
    return deployment


async def test_the_running_macd_deployment_moves_to_the_fresh_cross_version(db_engine, db_session):
    old_code = "# the 25 Sep MACD-line version\n"
    running = await _deployment(db_session, "macd_a@tradingmaster.internal", old_code)
    edited = await _deployment(db_session, "macd_b@tradingmaster.internal", "# edited by hand since\n")
    stopped = await _deployment(db_session, "macd_c@tradingmaster.internal", old_code, status=DeploymentStatus.STOPPED.value)
    before = {d.id: d.strategy_version_id for d in (running, edited, stopped)}
    running_id, edited_id, stopped_id = running.id, edited.id, stopped.id
    await db_session.commit()

    migration = _load("4abb8d69a900_macd_rsi_fresh_cross_version")
    migration.OLD_MD5 = hashlib.md5(old_code.encode()).hexdigest()
    await _run(db_engine, migration)
    await _run(db_engine, migration)  # a second run changes nothing
    db_session.expire_all()

    moved = await db_session.get(PaperNativeDeployment, running_id)
    version = await db_session.get(StrategyVersion, moved.strategy_version_id)
    assert version.version_number == 12 and version.python_code == BUILTIN.read_text(encoding="utf-8")
    assert (version.timeframe, version.parameters, version.position_sizing) == ("15m", {"x": 1}, {"type": "fixed_quantity", "value": 1})
    assert moved.state["holdings"]["SBIN"]["quantity"] == 10.0 and moved.state["seeded"] is True
    assert (await db_session.get(PaperNativeDeployment, edited_id)).strategy_version_id == before[edited_id]
    assert (await db_session.get(PaperNativeDeployment, stopped_id)).strategy_version_id == before[stopped_id]

    audit = (await db_session.execute(select(AuditLog))).scalars().all()
    assert [(a.action, a.object_id, a.previous_value, a.new_value["version_number"]) for a in audit] == [
        ("PAPER_NATIVE_VERSION_UPDATED", str(running_id), {"version_number": 11}, 12),
    ]


def test_the_built_in_is_the_approved_fresh_cross_code():
    migration = _load("4abb8d69a900_macd_rsi_fresh_cross_version")
    assert hashlib.md5(BUILTIN.read_bytes().replace(b"\r", b"")).hexdigest() == migration.NEW_MD5


# ------------------------------------------------- AM OP straddle at 3 PM --

IST = timezone(timedelta(hours=5, minutes=30))


async def _am_op(db, email: str, *, status: str, stopped_at, opened_at: datetime, legs: dict) -> tuple[PaperNativeDeployment, PaperPortfolio]:
    user = User(email=email, hashed_password="x", full_name="AM OP")
    db.add(user)
    await db.flush()
    strategy = Strategy(name="AM OP TRD 15 MIN", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                              python_code="#", position_sizing={}, risk_rules={})
    portfolio = PaperPortfolio(user_id=user.id, name="AM OP", cash=500000.0, initial_capital=500000.0)
    db.add_all([version, portfolio])
    await db.flush()
    position = {"regime": "sideways", "opened_at": opened_at.astimezone(timezone.utc).isoformat(), "legs": legs}
    deployment = PaperNativeDeployment(portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                       status=status, stopped_at=stopped_at, state={"position": position})
    db.add(deployment)
    await db.flush()
    return deployment, portfolio


async def _option(db, symbol: str, option_type: str, open_at_3pm: float | None) -> Instrument:
    nifty = (await db.execute(select(Instrument).where(Instrument.symbol == "NIFTY 50"))).scalar_one_or_none()
    if nifty is None:
        nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
        db.add(nifty)
        await db.flush()
    from datetime import date

    inst = Instrument(exchange="NFO", symbol=symbol, name=symbol, instrument_type="option", data_source="zerodha_kite", external_ref=symbol,
                      strike=22700.0, option_type=option_type, expiry=date(2026, 10, 6), lot_size=65, underlying_instrument_id=nifty.id)
    bf = BfSymbol(source="zerodha_nfo", symbol=symbol, display_name=symbol, option_type=option_type, underlying_symbol="NIFTY")
    db.add_all([inst, bf])
    await db.flush()
    if open_at_3pm is not None:
        ts = datetime(2026, 9, 30, 15, 0, tzinfo=IST).astimezone(timezone.utc)
        db.add(BfOhlcvBar(symbol_id=bf.id, timeframe="15m", ts=ts, open=open_at_3pm, high=open_at_3pm + 5, low=open_at_3pm - 5, close=open_at_3pm, volume=1000.0))
    return inst


def _leg(inst: Instrument, entry: float) -> dict:
    return {"instrument_id": str(inst.id), "strike": 22700.0, "option_type": inst.option_type, "side": "sell", "quantity": 650.0, "entry_price": entry}


async def test_the_stopped_am_op_straddle_closes_at_its_3pm_price(db_engine, db_session):
    from app.models.paper_trading import PaperNativeTrade

    ce = await _option(db_session, "NIFTY26O0622700CE", "CE", 142.5)
    pe = await _option(db_session, "NIFTY26O0622700PE", "PE", 131.0)
    opened = datetime(2026, 9, 30, 9, 52, tzinfo=IST)
    stopped = datetime(2026, 9, 30, 11, 14, 40, tzinfo=IST).astimezone(timezone.utc)  # SQLite keeps no offset: store UTC
    deployment, portfolio = await _am_op(db_session, "amop_a@tradingmaster.internal", status=DeploymentStatus.STOPPED.value,
                                         stopped_at=stopped, opened_at=opened, legs={"short_ce": _leg(ce, 150.0), "short_pe": _leg(pe, 125.0)})
    # Left alone: still running (its own rule closes it), and one whose price at 15:00 isn't on file.
    running, _ = await _am_op(db_session, "amop_b@tradingmaster.internal", status=DeploymentStatus.ACTIVE.value, stopped_at=None,
                              opened_at=opened, legs={"short_ce": _leg(ce, 150.0)})
    no_price = await _option(db_session, "NIFTY26O0622800CE", "CE", None)
    missing, _ = await _am_op(db_session, "amop_c@tradingmaster.internal", status=DeploymentStatus.STOPPED.value, stopped_at=stopped,
                              opened_at=opened, legs={"short_ce": _leg(ce, 150.0), "other": _leg(no_price, 90.0)})
    ids = (deployment.id, portfolio.id, running.id, missing.id)
    await db_session.commit()

    migration = _load("3cd892aa3d34_close_am_op_straddle_at_3pm")
    await _run(db_engine, migration)
    await _run(db_engine, migration)  # a second run finds nothing to close
    db_session.expire_all()

    trade = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == ids[0]))).scalar_one()
    assert trade.exit_reason == "time_cutoff_3pm"
    assert trade.closed_at.replace(tzinfo=timezone.utc) == datetime(2026, 9, 30, 9, 30, tzinfo=timezone.utc)
    assert [(leg["instrument_symbol"], leg["side"], leg["exit_price"]) for leg in trade.legs] == [
        ("NIFTY26O0622700CE", "short", 142.5), ("NIFTY26O0622700PE", "short", 131.0),
    ]
    assert trade.pnl == (150.0 - 142.5) * 650 + (125.0 - 131.0) * 650  # +4,875 - 3,900
    assert trade.charges is not None and trade.charges > 0
    assert (await db_session.get(PaperPortfolio, ids[1])).cash == 500000.0 - 650 * (142.5 + 131.0)
    closed = await db_session.get(PaperNativeDeployment, ids[0])
    assert closed.state["position"] is None and closed.status == "stopped" and closed.last_signal == "COVER"

    for other in ids[2:]:
        assert (await db_session.get(PaperNativeDeployment, other)).state["position"] is not None
        assert (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == other))).first() is None
