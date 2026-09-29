"""Migration d3e4f5a6b7c8: the 29 Sep 09:45 entries that read stale prices
(AM OP TRD 15 MIN's option legs, MACD - RSI - 15 MIN's ACUTAAS buy) become
the real 09:45 prices."""

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

MIGRATION = pathlib.Path(__file__).parent.parent / "alembic/versions/d3e4f5a6b7c8_correct_29_sep_stale_entries.py"
CANDLE = datetime(2026, 9, 29, 4, 15, tzinfo=timezone.utc)  # 09:45 IST
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


async def _deployment(db, name: str, state: dict) -> tuple[PaperNativeDeployment, PaperPortfolio]:
    user = User(email=f"{name.replace(' ', '').lower()}@tradingmaster.internal", hashed_password="x", full_name=name)
    db.add(user)
    await db.flush()
    strategy = Strategy(name=name, owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                              python_code="#", position_sizing={}, risk_rules={})
    portfolio = PaperPortfolio(user_id=user.id, name=name, cash=500000.0, initial_capital=500000.0)
    db.add_all([version, portfolio])
    await db.flush()
    deployment = PaperNativeDeployment(portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                       status=DeploymentStatus.ACTIVE.value, state=state)
    db.add(deployment)
    await db.flush()
    return deployment, portfolio


async def _instrument(db, symbol: str, price: float | None, *, ts: datetime = CANDLE, spread: float = 0.0) -> Instrument:
    """With a real 5m candle at `ts` opening at `price`, ranging price +- spread."""
    inst = Instrument(exchange="NFO" if symbol.startswith("NIFTY") else "NSE", symbol=symbol, name=symbol,
                      instrument_type="option" if symbol.startswith("NIFTY") else "equity", data_source="zerodha_kite", external_ref=symbol)
    db.add(inst)
    await db.flush()
    if price is not None:
        db.add(OhlcvCandle(instrument_id=inst.id, timeframe="5m", ts=ts, open=price, high=price + spread, low=price - spread,
                           close=price, source="bf"))
    return inst


async def _options_trade(db, *, with_candles: bool = True):
    deployment, portfolio = await _deployment(db, "AM OP TRD 15 MIN", {"position": None})
    short = await _instrument(db, "NIFTY26O0622800CE", 131.25 if with_candles else None)
    long_ = await _instrument(db, "NIFTY26O0623000CE", 64.10 if with_candles else None)
    trade = PaperNativeTrade(
        deployment_id=deployment.id, opened_at=datetime(2026, 9, 29, 4, 15, 8, tzinfo=timezone.utc),
        closed_at=datetime(2026, 9, 29, 9, 30, tzinfo=timezone.utc),
        legs=[
            {"instrument_id": str(short.id), "side": "short", "quantity": QTY, "entry_price": 206.60, "exit_price": 120.0},
            {"instrument_id": str(long_.id), "side": "long", "quantity": QTY, "entry_price": 109.00, "exit_price": 60.0},
        ],
        pnl=(206.60 - 120.0) * QTY + (60.0 - 109.00) * QTY, pnl_pct=0.0, exit_reason="time_cutoff_3pm",
    )
    db.add(trade)
    await db.commit()
    return portfolio.id, trade.id


async def test_the_closed_options_trade_and_cash_get_the_real_0945_prices(db_engine, db_session):
    portfolio_id, trade_id = await _options_trade(db_session)

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


async def test_open_macd_holdings_outside_the_real_range_get_the_real_price(db_engine, db_session):
    acutaas = await _instrument(db_session, "ACUTAAS", 3262.90, spread=10)
    delhivery = await _instrument(db_session, "DELHIVERY", 452.40, ts=datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc), spread=1.5)
    lauruslabs = await _instrument(db_session, "LAURUSLABS", 2002.00, ts=datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc), spread=5)
    amber = await _instrument(db_session, "AMBER", 6900.0)
    holdings = {
        "ACUTAAS": {"instrument_id": str(acutaas.id), "quantity": 122.0, "entry_price": 3203.20,
                    "opened_at": "2026-09-29T04:15:50.123456+00:00", "rsi_at_entry": 63.1},
        "DELHIVERY": {"instrument_id": str(delhivery.id), "quantity": 800.0, "entry_price": 449.80,
                      "opened_at": "2026-09-29T05:01:12+00:00", "rsi_at_entry": 58.0},
        # recorded inside the real candle's range: a genuine price, left alone
        "LAURUSLABS": {"instrument_id": str(lauruslabs.id), "quantity": 196.0, "entry_price": 2001.00,
                       "opened_at": "2026-09-28T06:00:30+00:00", "rsi_at_entry": 54.2},
        # not a target at all
        "AMBER": {"instrument_id": str(amber.id), "quantity": 56.0, "entry_price": 6911.50,
                  "opened_at": "2026-09-28T06:30:00+00:00", "rsi_at_entry": 60.6},
    }
    deployment, portfolio = await _deployment(db_session, "MACD - RSI - 15 MIN ", {"holdings": holdings})  # stored with a trailing space
    await db_session.commit()
    deployment_id, portfolio_id = deployment.id, portfolio.id

    await _run_migration(db_engine)
    await _run_migration(db_engine)

    db_session.expire_all()
    state = (await db_session.get(PaperNativeDeployment, deployment_id)).state
    assert state["holdings"]["ACUTAAS"] == {**holdings["ACUTAAS"], "entry_price": 3262.90}
    assert state["holdings"]["DELHIVERY"] == {**holdings["DELHIVERY"], "entry_price": 452.40}
    assert state["holdings"]["LAURUSLABS"] == holdings["LAURUSLABS"]
    assert state["holdings"]["AMBER"] == holdings["AMBER"]
    # Each buy debited qty x recorded price; at the real price it costs qty x the difference more.
    expected = 500000.0 - (3262.90 - 3203.20) * 122 - (452.40 - 449.80) * 800
    assert round((await db_session.get(PaperPortfolio, portfolio_id)).cash, 2) == round(expected, 2)
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "PAPER_NATIVE_ENTRY_CORRECTED"))).scalars().all()
    assert sorted(next(iter(log.new_value["entries"])) for log in logs) == ["ACUTAAS", "DELHIVERY"]


async def test_nothing_changes_without_the_real_candles(db_engine, db_session):
    portfolio_id, trade_id = await _options_trade(db_session, with_candles=False)

    await _run_migration(db_engine)

    db_session.expire_all()
    assert [leg["entry_price"] for leg in (await db_session.get(PaperNativeTrade, trade_id)).legs] == [206.60, 109.00]
    assert (await db_session.get(PaperPortfolio, portfolio_id)).cash == 500000.0
