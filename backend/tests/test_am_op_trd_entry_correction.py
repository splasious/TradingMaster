"""Migration d3e4f5a6b7c8: AM OP TRD 15 MIN's 29 Sep 09:45 entries, which
read 28 Sep's close, become the real 09:45 prices."""

import importlib.util
import pathlib
from datetime import datetime, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User

MIGRATION = pathlib.Path(__file__).parent.parent / "alembic/versions/d3e4f5a6b7c8_correct_am_op_trd_29_sep_entries.py"
OPENED = datetime(2026, 9, 29, 4, 15, 8, tzinfo=timezone.utc)
CANDLE = datetime(2026, 9, 29, 4, 15, tzinfo=timezone.utc)
QTY = 650.0


async def _run_migration(db_engine) -> None:
    spec = importlib.util.spec_from_file_location("correct_entries", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    async with db_engine.begin() as conn:
        await conn.run_sync(run)


async def _setup(db, *, with_candles: bool = True):
    user = User(email="amop@tradingmaster.internal", hashed_password="x", full_name="AM OP")
    db.add(user)
    await db.flush()
    strategy = Strategy(name="AM OP TRD 15 MIN", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=6, timeframe="15m", instrument_ids=[], parameters={},
                              python_code="#", position_sizing={}, risk_rules={})
    portfolio = PaperPortfolio(user_id=user.id, name="AM OP TRD 15 MIN", cash=500000.0, initial_capital=500000.0)
    db.add_all([version, portfolio])
    await db.flush()
    deployment = PaperNativeDeployment(portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                       status=DeploymentStatus.ACTIVE.value, state={"position": None})
    short = Instrument(exchange="NFO", symbol="NIFTY26O0622800CE", name="x", instrument_type="option", data_source="zerodha_kite", external_ref="a")
    long_ = Instrument(exchange="NFO", symbol="NIFTY26O0623000CE", name="x", instrument_type="option", data_source="zerodha_kite", external_ref="b")
    db.add_all([deployment, short, long_])
    await db.flush()
    if with_candles:
        for inst, price in ((short, 131.25), (long_, 64.10)):
            db.add(OhlcvCandle(instrument_id=inst.id, timeframe="5m", ts=CANDLE, open=price, high=price, low=price, close=price, source="bf"))
    trade = PaperNativeTrade(
        deployment_id=deployment.id, opened_at=OPENED, closed_at=datetime(2026, 9, 29, 9, 30, tzinfo=timezone.utc),
        legs=[
            {"instrument_id": str(short.id), "side": "short", "quantity": QTY, "entry_price": 206.60, "exit_price": 120.0},
            {"instrument_id": str(long_.id), "side": "long", "quantity": QTY, "entry_price": 109.00, "exit_price": 60.0},
        ],
        pnl=(206.60 - 120.0) * QTY + (60.0 - 109.00) * QTY, pnl_pct=0.0, exit_reason="time_cutoff_3pm",
    )
    db.add(trade)
    await db.commit()
    return portfolio.id, trade.id


async def test_the_closed_trade_and_cash_get_the_real_0945_prices(db_engine, db_session):
    portfolio_id, trade_id = await _setup(db_session)

    await _run_migration(db_engine)
    await _run_migration(db_engine)  # a second run changes nothing

    db_session.expire_all()
    fixed = await db_session.get(PaperNativeTrade, trade_id)
    assert [leg["entry_price"] for leg in fixed.legs] == [131.25, 64.10]
    assert round(fixed.pnl, 2) == round((131.25 - 120.0) * QTY + (60.0 - 64.10) * QTY, 2)
    # Opening credited 206.60 and debited 109.00 per unit; the real prices credit 131.25 and debit 64.10.
    assert round((await db_session.get(PaperPortfolio, portfolio_id)).cash, 2) == round(500000.0 + (131.25 - 206.60) * QTY - (64.10 - 109.00) * QTY, 2)
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "PAPER_NATIVE_ENTRY_CORRECTED"))).scalars().all()
    assert len(logs) == 1 and logs[0].new_value["entries"] == {"NIFTY26O0622800CE": 131.25, "NIFTY26O0623000CE": 64.10}


async def test_nothing_changes_without_the_real_candles(db_engine, db_session):
    portfolio_id, trade_id = await _setup(db_session, with_candles=False)

    await _run_migration(db_engine)

    db_session.expire_all()
    assert [leg["entry_price"] for leg in (await db_session.get(PaperNativeTrade, trade_id)).legs] == [206.60, 109.00]
    assert (await db_session.get(PaperPortfolio, portfolio_id)).cash == 500000.0
