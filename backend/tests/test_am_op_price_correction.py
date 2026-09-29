"""Migration e4f5a6b7c8d9: AM OP TRD 15 MIN's option prices that lie outside
their real 15-minute candle (NIFTY weeklies have no 5-minute candles on file)
become that candle's open -- entries and exits alike."""

import importlib.util
import pathlib
from datetime import datetime, timezone

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.paper_trading.trade_record import estimate_charges

MIGRATION = pathlib.Path(__file__).parent.parent / "alembic/versions/e4f5a6b7c8d9_correct_am_op_option_prices.py"
UTC = timezone.utc
QTY = 650.0


async def _run_migration(db_engine) -> None:
    spec = importlib.util.spec_from_file_location("correct_am_op", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    async with db_engine.begin() as conn:
        await conn.run_sync(run)


async def _deployment(db, name: str) -> tuple[PaperNativeDeployment, PaperPortfolio]:
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
                                       status=DeploymentStatus.ACTIVE.value, state={"position": None})
    db.add(deployment)
    await db.flush()
    return deployment, portfolio


async def _option(db, symbol: str, candles: dict[datetime, tuple[float, float, float]], *, backfill_only: bool = False) -> Instrument:
    """With real 15m candles {start: (open, low, high)} in the chart table, or only in the backfill's copy."""
    inst = Instrument(exchange="NFO", symbol=symbol, name=symbol, instrument_type="option", option_type=symbol[-2:],
                      data_source="zerodha_kite", external_ref=symbol)
    db.add(inst)
    await db.flush()
    if backfill_only:
        bf = BfSymbol(source="zerodha_nfo", symbol=symbol, display_name=symbol)
        db.add(bf)
        await db.flush()
    for ts, (o, lo, hi) in candles.items():
        if backfill_only:
            db.add(BfOhlcvBar(symbol_id=bf.id, timeframe="15m", ts=ts, open=o, high=hi, low=lo, close=o))
        else:
            db.add(OhlcvCandle(instrument_id=inst.id, timeframe="15m", ts=ts, open=o, high=hi, low=lo, close=o, source="bf"))
    return inst


def _leg(inst: Instrument, side: str, entry: float, exit_price: float) -> dict:
    return {"instrument_id": str(inst.id), "side": side, "quantity": QTY, "entry_price": entry, "exit_price": exit_price,
            "instrument_type": "option", "option_type": inst.option_type, "exchange": "NFO"}


def _trade(deployment, opened: datetime, closed: datetime, legs: list[dict]) -> PaperNativeTrade:
    pnl = sum(((l["entry_price"] - l["exit_price"]) if l["side"] == "short" else (l["exit_price"] - l["entry_price"])) * l["quantity"] for l in legs)
    return PaperNativeTrade(deployment_id=deployment.id, opened_at=opened, closed_at=closed, legs=legs, pnl=pnl, pnl_pct=0.0,
                            charges=estimate_charges(legs, opened, closed), exit_reason="pcr_exit_0.801")


async def test_the_29_sep_entries_become_the_real_15m_open(db_engine, db_session):
    at_0945 = datetime(2026, 9, 29, 4, 15, tzinfo=UTC)
    at_1115 = datetime(2026, 9, 29, 5, 45, tzinfo=UTC)
    deployment, portfolio = await _deployment(db_session, "AM OP TRD 15 MIN")
    short = await _option(db_session, "NIFTY26O0622800CE", {at_0945: (129.95, 121.30, 131.70), at_1115: (95.0, 90.0, 100.0)})
    long_ = await _option(db_session, "NIFTY26O0623000CE", {at_0945: (65.00, 60.15, 65.60), at_1115: (44.0, 42.0, 46.0)})
    trade = _trade(deployment, datetime(2026, 9, 29, 4, 15, 8, tzinfo=UTC), datetime(2026, 9, 29, 5, 45, 12, tzinfo=UTC),
                   [_leg(short, "short", 206.60, 95.50), _leg(long_, "long", 109.00, 44.20)])  # exits inside the 11:15 range
    db_session.add(trade)
    await db_session.commit()
    trade_id, portfolio_id, old_charges = trade.id, portfolio.id, trade.charges

    await _run_migration(db_engine)
    await _run_migration(db_engine)  # a second run changes nothing

    db_session.expire_all()
    fixed = await db_session.get(PaperNativeTrade, trade_id)
    assert [(l["entry_price"], l["exit_price"]) for l in fixed.legs] == [(129.95, 95.50), (65.00, 44.20)]
    pnl = (129.95 - 95.50) * QTY + (44.20 - 65.00) * QTY
    assert round(fixed.pnl, 2) == round(pnl, 2)
    assert round(fixed.pnl_pct, 4) == round(pnl / ((129.95 + 65.00) * QTY) * 100, 4)
    assert fixed.charges == estimate_charges(fixed.legs, fixed.opened_at, fixed.closed_at) != old_charges
    # Opening credited 206.60 and debited 109.00 per unit; the real prices credit 129.95 and debit 65.00.
    cash = 500000.0 + (129.95 - 206.60) * QTY - (65.00 - 109.00) * QTY
    assert round((await db_session.get(PaperPortfolio, portfolio_id)).cash, 2) == round(cash, 2)
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "PAPER_NATIVE_PRICE_CORRECTED"))).scalars().all()
    assert len(logs) == 1
    assert logs[0].previous_value == {"entries": {"NIFTY26O0622800CE": 206.60, "NIFTY26O0623000CE": 109.00}, "exits": {}}
    assert logs[0].new_value["entries"] == {"NIFTY26O0622800CE": 129.95, "NIFTY26O0623000CE": 65.00}


async def test_25_sep_1404_exits_and_entries_from_the_backfill_copy(db_engine, db_session):
    at_1400 = datetime(2026, 9, 25, 8, 30, tzinfo=UTC)  # 14:00 IST
    deployment, portfolio = await _deployment(db_session, "AM OP TRD 15 MIN ")  # surrounding spaces don't matter
    a_long = await _option(db_session, "NIFTY26S3023100CE", {at_1400: (20.0, 18.0, 22.0)}, backfill_only=True)
    a_short = await _option(db_session, "NIFTY26S3022900CE", {at_1400: (60.0, 55.0, 65.0)}, backfill_only=True)
    b_long = await _option(db_session, "NIFTY26S3022700PE", {at_1400: (30.0, 28.0, 33.0)}, backfill_only=True)
    b_short = await _option(db_session, "NIFTY26S3022900PE", {at_1400: (70.0, 66.0, 75.0)}, backfill_only=True)
    morning = _trade(deployment, datetime(2026, 9, 25, 4, 15, 7, tzinfo=UTC), datetime(2026, 9, 25, 8, 34, 18, tzinfo=UTC),
                     [_leg(a_long, "long", 25.0, 39.54), _leg(a_short, "short", 50.0, 118.34)])  # exits about double
    afternoon = _trade(deployment, datetime(2026, 9, 25, 8, 34, 16, tzinfo=UTC), datetime(2026, 9, 25, 9, 30, 4, tzinfo=UTC),
                       [_leg(b_long, "long", 54.03, 29.0), _leg(b_short, "short", 145.76, 71.0)])  # entries 80% / 108% over
    db_session.add_all([morning, afternoon])
    await db_session.commit()
    ids, portfolio_id = (morning.id, afternoon.id), portfolio.id

    await _run_migration(db_engine)
    await _run_migration(db_engine)

    db_session.expire_all()
    m, a = [await db_session.get(PaperNativeTrade, i) for i in ids]
    assert [(l["entry_price"], l["exit_price"]) for l in m.legs] == [(25.0, 20.0), (50.0, 60.0)]
    assert [(l["entry_price"], l["exit_price"]) for l in a.legs] == [(30.0, 29.0), (70.0, 71.0)]
    assert round(m.pnl, 2) == round((20.0 - 25.0) * QTY + (50.0 - 60.0) * QTY, 2)
    assert round(a.pnl, 2) == round((29.0 - 30.0) * QTY + (70.0 - 71.0) * QTY, 2)
    # Closing the long credited 39.54 (now 20.00) and covering the short debited 118.34 (now 60.00);
    # opening the long debited 54.03 (now 30.00) and the short credited 145.76 (now 70.00).
    cash = 500000.0 + (20.0 - 39.54) * QTY - (60.0 - 118.34) * QTY - (30.0 - 54.03) * QTY + (70.0 - 145.76) * QTY
    assert round((await db_session.get(PaperPortfolio, portfolio_id)).cash, 2) == round(cash, 2)
    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "PAPER_NATIVE_PRICE_CORRECTED"))).scalars().all()
    assert sorted((sorted(log.new_value["entries"]), sorted(log.new_value["exits"])) for log in logs) == [
        ([], ["NIFTY26S3022900CE", "NIFTY26S3023100CE"]), (["NIFTY26S3022700PE", "NIFTY26S3022900PE"], []),
    ]


async def test_prices_in_range_outside_the_windows_or_other_strategies_are_left(db_engine, db_session):
    at_0945 = datetime(2026, 9, 29, 4, 15, tzinfo=UTC)
    am_op, am_portfolio = await _deployment(db_session, "AM OP TRD 15 MIN")
    other, other_portfolio = await _deployment(db_session, "Some Other Strategy")
    inst = await _option(db_session, "NIFTY26O0622800CE", {at_0945: (129.95, 121.30, 131.70)})
    no_candle = await _option(db_session, "NIFTY26O0622900CE", {})
    trades = [
        _trade(am_op, datetime(2026, 9, 29, 4, 15, 8, tzinfo=UTC), datetime(2026, 9, 29, 9, 30, tzinfo=UTC),
               [_leg(inst, "short", 130.50, 100.0)]),  # inside the range: a real price
        _trade(am_op, datetime(2026, 9, 29, 4, 21, 0, tzinfo=UTC), datetime(2026, 9, 29, 9, 30, tzinfo=UTC),
               [_leg(inst, "short", 206.60, 100.0)]),  # 09:51, past the candle's first 5 minutes
        _trade(am_op, datetime(2026, 9, 29, 4, 15, 8, tzinfo=UTC), datetime(2026, 9, 29, 9, 30, tzinfo=UTC),
               [_leg(no_candle, "short", 206.60, 100.0)]),  # no candle on file
        _trade(other, datetime(2026, 9, 29, 4, 15, 8, tzinfo=UTC), datetime(2026, 9, 29, 9, 30, tzinfo=UTC),
               [_leg(inst, "short", 206.60, 100.0)]),  # another strategy
    ]
    db_session.add_all(trades)
    await db_session.commit()
    ids, portfolio_ids = [t.id for t in trades], (am_portfolio.id, other_portfolio.id)

    await _run_migration(db_engine)

    db_session.expire_all()
    assert [(await db_session.get(PaperNativeTrade, i)).legs[0]["entry_price"] for i in ids] == [130.50, 206.60, 206.60, 206.60]
    assert [(await db_session.get(PaperPortfolio, p)).cash for p in portfolio_ids] == [500000.0, 500000.0]
    assert (await db_session.execute(select(AuditLog).where(AuditLog.action == "PAPER_NATIVE_PRICE_CORRECTED"))).first() is None
