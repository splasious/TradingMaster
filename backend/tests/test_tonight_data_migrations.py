"""Migrations of 30 Sep evening: a6b7c8d9e0f1 marks for one more copy the NFO
contracts whose live-feed candles still differ from Kite's final ones, and
4abb8d69a900 moves MACD - RSI - 15 MIN's running deployment onto the fresh
up-cross version, keeping its holdings; on 1 Oct, cc8323c6d54e moves it and
RS Rotation 15 MIN onto their 57-stock lists, and 7dab5ef6e158 RS Rotation
15 MIN onto the version that carries a close into an empty slot."""

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
    migration.NEW_MD5 = _md5(BUILTIN)  # the built-in has moved on since (cc8323c6d54e)
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


def _md5(path: pathlib.Path) -> str:
    return hashlib.md5(path.read_bytes().replace(b"\r", b"")).hexdigest()


def test_the_built_ins_are_the_approved_code():
    fresh_cross = _load("4abb8d69a900_macd_rsi_fresh_cross_version")
    seven = _load("cc8323c6d54e_add_seven_stocks_to_15min_lists")
    carry = _load("7dab5ef6e158_rs_15min_carry_close_into_empty_slot")
    assert seven.CHANGES["macd_rsi_15min.py"][0] == fresh_cross.NEW_MD5  # each moves on from the version before
    assert seven.CHANGES["nifty_rs_rotation_15min.py"][1] in carry.CHANGES["nifty_rs_rotation_15min.py"][0]
    assert _md5(BUILTIN) == seven.CHANGES["macd_rsi_15min.py"][1]
    assert _md5(BUILTIN.parent / "nifty_rs_rotation_15min.py") == carry.CHANGES["nifty_rs_rotation_15min.py"][1]


async def test_running_15min_deployments_move_to_the_57_stock_lists(db_engine, db_session):
    macd_code, rs_code = "# MACD, 50 stocks\n", "# RS 15, 50 stocks\n"
    macd = await _deployment(db_session, "seven_a@tradingmaster.internal", macd_code)
    rs = await _deployment(db_session, "seven_b@tradingmaster.internal", rs_code)
    edited = await _deployment(db_session, "seven_c@tradingmaster.internal", "# edited by hand since\n")
    stopped = await _deployment(db_session, "seven_d@tradingmaster.internal", macd_code, status=DeploymentStatus.STOPPED.value)
    before = {d.id: d.strategy_version_id for d in (macd, rs, edited, stopped)}
    ids = [d.id for d in (macd, rs, edited, stopped)]
    await db_session.commit()

    migration = _load("cc8323c6d54e_add_seven_stocks_to_15min_lists")
    migration.CHANGES = {
        "macd_rsi_15min.py": (hashlib.md5(macd_code.encode()).hexdigest(), migration.CHANGES["macd_rsi_15min.py"][1]),
        # the RS built-in has moved on since (7dab5ef6e158)
        "nifty_rs_rotation_15min.py": (hashlib.md5(rs_code.encode()).hexdigest(), _md5(BUILTIN.parent / "nifty_rs_rotation_15min.py")),
    }
    await _run(db_engine, migration)
    await _run(db_engine, migration)  # a second run changes nothing
    db_session.expire_all()

    for deployment_id, filename in ((ids[0], "macd_rsi_15min.py"), (ids[1], "nifty_rs_rotation_15min.py")):
        moved = await db_session.get(PaperNativeDeployment, deployment_id)
        version = await db_session.get(StrategyVersion, moved.strategy_version_id)
        assert version.version_number == 12 and version.python_code == (BUILTIN.parent / filename).read_text(encoding="utf-8")
        assert '"WELCORP"' in version.python_code and version.parameters == {"x": 1}
        assert moved.state["holdings"]["SBIN"]["quantity"] == 10.0
    for deployment_id in ids[2:]:
        assert (await db_session.get(PaperNativeDeployment, deployment_id)).strategy_version_id == before[deployment_id]

    audit = (await db_session.execute(select(AuditLog))).scalars().all()
    assert sorted((a.object_id, a.previous_value["version_number"], a.new_value["version_number"]) for a in audit) == sorted(
        [(str(ids[0]), 11, 12), (str(ids[1]), 11, 12)]
    )



async def test_a_running_rs_15min_deployment_moves_to_the_carry_forward_version(db_engine, db_session):
    fifty, fifty_seven = "# RS 15, 50 stocks\n", "# RS 15, 57 stocks\n"
    on_50 = await _deployment(db_session, "carry_a@tradingmaster.internal", fifty)
    on_57 = await _deployment(db_session, "carry_b@tradingmaster.internal", fifty_seven)
    edited = await _deployment(db_session, "carry_c@tradingmaster.internal", "# edited by hand since\n")
    ids, before = [on_50.id, on_57.id, edited.id], edited.strategy_version_id
    await db_session.commit()

    migration = _load("7dab5ef6e158_rs_15min_carry_close_into_empty_slot")
    to_md5 = migration.CHANGES["nifty_rs_rotation_15min.py"][1]
    migration.CHANGES = {"nifty_rs_rotation_15min.py": ({hashlib.md5(c.encode()).hexdigest() for c in (fifty, fifty_seven)}, to_md5)}
    await _run(db_engine, migration)
    await _run(db_engine, migration)  # a second run changes nothing
    db_session.expire_all()

    rs_code = (BUILTIN.parent / "nifty_rs_rotation_15min.py").read_text(encoding="utf-8")
    for deployment_id in ids[:2]:
        moved = await db_session.get(PaperNativeDeployment, deployment_id)
        version = await db_session.get(StrategyVersion, moved.strategy_version_id)
        assert version.version_number == 12 and version.python_code == rs_code and "CARRY_BACK" in rs_code
        assert moved.state["holdings"]["SBIN"]["quantity"] == 10.0
    assert (await db_session.get(PaperNativeDeployment, ids[2])).strategy_version_id == before


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


# ------------------------------------------- AM OP: roll on 15-minute closes --

AM_OP = pathlib.Path(__file__).parent.parent / "app/services/strategy/native_strategies/nifty_pcr_multi_regime.py"


async def test_the_running_am_op_deployment_moves_to_the_15min_close_roll_version(db_engine, db_session):
    from app.models.broker import Broker, BrokerAccount
    from app.models.live_native import LiveNativeDeployment

    old_code = "# AM OP version 6: rolls on the live price\n"
    running = await _deployment(db_session, "amop_a@tradingmaster.internal", old_code)
    edited = await _deployment(db_session, "amop_b@tradingmaster.internal", "# edited by hand since\n")
    other = await _deployment(db_session, "amop_c@tradingmaster.internal", old_code)
    for deployment, name in ((running, "AM OP TRD 15 MIN"), (edited, "AM OP TRD 15 MIN"), (other, "MACD - RSI - 15 MIN")):
        (await db_session.get(Strategy, deployment.strategy_id)).name = name
    running.state = {"position": {"regime": "sideways", "entry_spot": 22480.4, "legs": {}}}
    broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    owner = (await db_session.get(PaperPortfolio, running.portfolio_id)).user_id
    account = BrokerAccount(user_id=owner, broker_id=broker.id, account_label="Kite", environment="live")
    db_session.add(account)
    await db_session.flush()
    live = LiveNativeDeployment(owner_id=owner, strategy_id=running.strategy_id, strategy_version_id=running.strategy_version_id,
                                broker_account_id=account.id, paper_deployment_id=running.id, status="active", capital=300000)
    db_session.add(live)
    await db_session.flush()
    before = {d.id: d.strategy_version_id for d in (running, edited, other)}
    running_id, edited_id, other_id, live_id = running.id, edited.id, other.id, live.id
    await db_session.commit()

    migration = _load("5c8e1f2a9b3d_am_op_roll_on_15min_close")
    migration.NEW_MD5 = _md5(AM_OP)  # the built-in went back to the live-move roll since (7e1a3c5b8d20)
    migration.OLD_MD5 = hashlib.md5(old_code.encode()).hexdigest()
    await _run(db_engine, migration)
    await _run(db_engine, migration)  # a second run changes nothing
    db_session.expire_all()

    moved = await db_session.get(PaperNativeDeployment, running_id)
    version = await db_session.get(StrategyVersion, moved.strategy_version_id)
    assert version.version_number == 12 and version.python_code == AM_OP.read_text(encoding="utf-8")
    assert moved.state["position"]["entry_spot"] == 22480.4  # its position carries on
    assert (await db_session.get(LiveNativeDeployment, live_id)).strategy_version_id == version.id
    assert (await db_session.get(PaperNativeDeployment, edited_id)).strategy_version_id == before[edited_id]
    assert (await db_session.get(PaperNativeDeployment, other_id)).strategy_version_id == before[other_id]
    audit = (await db_session.execute(select(AuditLog))).scalars().all()
    assert [(a.object_id, a.previous_value, a.new_value["version_number"]) for a in audit] == [(str(running_id), {"version_number": 11}, 12)]


async def test_am_op_goes_back_to_its_version_6_code_unchanged(db_engine, db_session):
    v6, v7 = "# AM OP version 6: rolls on the live 100-point move\n", "# AM OP version 7: rolls on a 15-minute close\n"
    running = await _deployment(db_session, "amop_v7a@tradingmaster.internal", v6)  # saved as version 11 here
    (await db_session.get(Strategy, running.strategy_id)).name = "AM OP TRD 15 MIN"
    v7_row = StrategyVersion(strategy_id=running.strategy_id, version_number=12, timeframe="15m", instrument_ids=[], parameters={"x": 1},
                             python_code=v7, position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={})
    db_session.add(v7_row)
    await db_session.flush()
    running.strategy_version_id = v7_row.id
    running.state = {"position": None, "seeded": True}
    no_v6 = await _deployment(db_session, "amop_v7b@tradingmaster.internal", v7)  # on version 7, but no version 6 to go back to
    (await db_session.get(Strategy, no_v6.strategy_id)).name = "AM OP TRD 15 MIN"
    running_id, no_v6_id, no_v6_version = running.id, no_v6.id, no_v6.strategy_version_id
    await db_session.commit()

    migration = _load("7e1a3c5b8d20_am_op_back_to_live_100pt_roll")
    migration.V6_MD5 = hashlib.md5(v6.encode()).hexdigest()
    migration.V7_MD5 = hashlib.md5(v7.encode()).hexdigest()
    await _run(db_engine, migration)
    await _run(db_engine, migration)  # a second run changes nothing
    db_session.expire_all()

    moved = await db_session.get(PaperNativeDeployment, running_id)
    version = await db_session.get(StrategyVersion, moved.strategy_version_id)
    assert version.version_number == 13 and version.python_code == v6 and moved.state == {"position": None, "seeded": True}
    assert (await db_session.get(PaperNativeDeployment, no_v6_id)).strategy_version_id == no_v6_version
    audit = (await db_session.execute(select(AuditLog))).scalars().all()
    assert [(a.object_id, a.previous_value, a.new_value["version_number"]) for a in audit] == [(str(running_id), {"version_number": 12}, 13)]


def test_the_am_op_built_in_rolls_on_the_live_move_again():
    code = AM_OP.read_text(encoding="utf-8")
    assert "moved >= ROLL_TRIGGER" in code and "CLOSE_GRACE" not in code
