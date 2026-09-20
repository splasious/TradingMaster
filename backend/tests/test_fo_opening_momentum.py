import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from app.services.strategy.native_strategies.fo_opening_momentum import (
    evaluate,
    in_expiry_blackout,
    momentum_direction,
    nifty_filter_allows,
    opening_strength_ok,
    passes_momentum,
    passes_oi_change,
    passes_retracement,
    retracement_pct,
    sma_exit_triggered,
)


def test_momentum_direction_and_threshold():
    assert momentum_direction(3.0) == "CE"
    assert momentum_direction(-3.0) == "PE"
    assert passes_momentum(2.01) is True
    assert passes_momentum(2.0) is False
    assert passes_momentum(-2.5) is True


def test_oi_change():
    assert passes_oi_change(7.1) is True
    assert passes_oi_change(-7.1) is True
    assert passes_oi_change(7.0) is False
    assert passes_oi_change(None) is False


def test_opening_strength_tolerance():
    assert opening_strength_ok(100.0, 101.0, 100.0, "CE") is True  # open == low
    assert opening_strength_ok(100.05, 101.0, 100.0, "CE") is True  # within 0.05%
    assert opening_strength_ok(100.2, 101.0, 100.0, "CE") is False  # beyond tolerance
    assert opening_strength_ok(101.0, 101.0, 100.0, "PE") is True  # open == high
    assert opening_strength_ok(0.0, 1.0, 0.0, "CE") is False  # guard against zero open


def test_retracement():
    # CE: retraces from the High. Half-range retrace == exactly 50% -> fails (>= boundary)
    assert retracement_pct(110.0, 100.0, 105.0, "CE") == 50.0
    assert passes_retracement(110.0, 100.0, 105.0, "CE") is False
    assert passes_retracement(110.0, 100.0, 106.0, "CE") is True  # < 50%
    # PE: retraces from the Low
    assert retracement_pct(110.0, 100.0, 105.0, "PE") == 50.0
    assert passes_retracement(110.0, 100.0, 104.0, "PE") is True
    # doji (no range) -> 0% retracement, always passes
    assert retracement_pct(100.0, 100.0, 100.0, "CE") == 0.0


def test_nifty_filter():
    assert nifty_filter_allows("CE", nifty_open=100, nifty_close=101) is True  # green -> gainers ok
    assert nifty_filter_allows("PE", nifty_open=100, nifty_close=101) is True  # green -> losers ok too
    assert nifty_filter_allows("CE", nifty_open=101, nifty_close=100) is False  # red -> gainers excluded
    assert nifty_filter_allows("PE", nifty_open=101, nifty_close=100) is True  # red -> losers still ok


def test_sma_exit():
    # 8-period SMA, need 2 consecutive closes below it for a CE exit
    rising = [100 + i for i in range(9)]  # closes above their own trailing SMA throughout
    assert sma_exit_triggered("CE", rising) is False
    falling_then_below = [100, 101, 102, 103, 104, 105, 106, 107, 90, 88]
    assert sma_exit_triggered("CE", falling_then_below) is True
    assert sma_exit_triggered("PE", falling_then_below) is False
    # not enough candles yet
    assert sma_exit_triggered("CE", [100, 99, 98]) is False


def test_expiry_blackout_window():
    expiry = date(2026, 9, 29)  # Tuesday
    assert in_expiry_blackout(date(2026, 9, 29), expiry) is True  # expiry day itself
    assert in_expiry_blackout(date(2026, 9, 25), expiry) is True  # 2 trading days before (Fri)
    assert in_expiry_blackout(date(2026, 10, 1), expiry) is True  # 2 trading days after (Thu)
    assert in_expiry_blackout(date(2026, 9, 22), expiry) is False  # a full week before, clear
    assert in_expiry_blackout(date(2026, 10, 6), expiry) is False  # a week after, clear


async def _setup(db_session: AsyncSession, *, cash: float = 1000000.0):
    role = Role(name=f"native_fo_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"native_fo_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Native FO User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    strategy = Strategy(name="F&O Opening Momentum", owner_id=user.id, code_type="native")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="5m", instrument_ids=[], parameters={},
        entry_rules=None, exit_rules=None, python_code="# see native_strategies/fo_opening_momentum.py",
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=cash, initial_capital=cash)
    db_session.add(portfolio)
    await db_session.flush()

    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
        status=DeploymentStatus.ACTIVE.value, state=None,
    )
    db_session.add(deployment)

    nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    equity = Instrument(exchange="NSE", symbol="TESTCO", name="Test Co", instrument_type="equity", data_source="zerodha_kite", external_ref="TESTCO")
    db_session.add_all([nifty, equity])
    await db_session.flush()

    future = Instrument(
        exchange="NFO", symbol="TESTCO26SEPFUT", name="TESTCO FUT", instrument_type="future", data_source="zerodha_kite",
        external_ref="TESTCO26SEPFUT", expiry=date(2026, 9, 29), lot_size=500, underlying_instrument_id=equity.id,
    )
    db_session.add(future)
    await db_session.flush()

    return {"user": user, "portfolio": portfolio, "deployment": deployment, "nifty": nifty, "equity": equity, "future": future}


def _candle(instrument_id, ts_ist: datetime, o, h, l, c, oi=None, timeframe="5m"):
    return OhlcvCandle(
        instrument_id=instrument_id, timeframe=timeframe, ts=ts_ist.astimezone(timezone.utc),
        open=o, high=h, low=l, close=c, volume=0, open_interest=oi, source="test",
    )


async def test_scan_shortlists_and_enters_on_breakout_then_exits_on_sma(db_session: AsyncSession):
    ctx_data = await _setup(db_session)
    equity, nifty, future = ctx_data["equity"], ctx_data["nifty"], ctx_data["future"]
    day = date(2026, 9, 21)  # Monday
    prev_day = date(2026, 9, 18)

    # previous session's close (for % move) and EOD OI (for the OI filter)
    db_session.add(_candle(equity.id, datetime.combine(prev_day, dtime(15, 25), tzinfo=IST), 95, 96, 94, 95.0))
    db_session.add(_candle(future.id, datetime.combine(prev_day, dtime(15, 25), tzinfo=IST), 95, 96, 94, 95.0, oi=100000))

    # 9:15-9:20 opening candle: strong CE setup -- open==low, closes near the high (no retracement)
    db_session.add(_candle(equity.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 100.0, 102.0, 100.0, 101.9))
    # 9:20-9:25: extends the range up to the 9:25 breakout level
    db_session.add(_candle(equity.id, datetime.combine(day, dtime(9, 20), tzinfo=IST), 101.9, 103.0, 101.5, 102.5))
    # today's latest futures OI, +8% vs prior session -> passes Step 3
    db_session.add(_candle(future.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 95, 96, 94, 95.0, oi=108000))

    # Nifty green 9:15-9:20 -> gainers allowed
    db_session.add(_candle(nifty.id, datetime.combine(day, dtime(9, 15), tzinfo=IST), 25000, 25050, 24990, 25040))

    await db_session.commit()

    tick_engine.set_real_price(equity.id, 103.5, "test")  # +9% vs prev close 95 -> passes momentum

    # ---- 9:20 scan ----
    ctx = NativeContext(db=db_session, portfolio=ctx_data["portfolio"], deployment=ctx_data["deployment"], state={}, now=datetime.combine(day, dtime(9, 20), tzinfo=IST))
    await evaluate(ctx)
    assert "TESTCO" in ctx.state["setups"]
    setup = ctx.state["setups"]["TESTCO"]
    assert setup["direction"] == "CE"
    assert setup["status"] == "watching"

    # ---- 9:25 breakout mark + entry (spot already above the 9:15-9:25 high of 103.0) ----
    option = Instrument(
        exchange="NFO", symbol="TESTCO26SEP106CE", name="TESTCO 106 CE", instrument_type="option", data_source="zerodha_kite",
        external_ref="TESTCO26SEP106CE", strike=106.0, option_type="CE", expiry=date(2026, 9, 29), lot_size=500,
        underlying_instrument_id=equity.id,
    )
    db_session.add(option)
    await db_session.flush()
    tick_engine.set_real_price(option.id, 5.0, "test")

    ctx.now = datetime.combine(day, dtime(9, 30), tzinfo=IST)
    starting_cash = ctx.portfolio.cash
    await evaluate(ctx)
    setup = ctx.state["setups"]["TESTCO"]
    assert setup["status"] == "triggered"
    assert setup["option_symbol"] == "TESTCO26SEP106CE"
    assert setup["entry_premium"] == 5.0
    assert ctx.portfolio.cash == starting_cash - 5.0 * 500  # bought 1 lot

    # ---- SMA exit: seed a falling run of 5m closes on the underlying, then check ----
    closes = [102, 101, 100, 99, 98, 97, 96, 95, 80, 78]
    candle_start = datetime.combine(day, dtime(9, 35), tzinfo=IST)
    for i, c in enumerate(closes):
        ts = candle_start + timedelta(minutes=5 * i)
        db_session.add(_candle(equity.id, ts, c - 1, c + 1, c - 1, c))
    await db_session.commit()
    tick_engine.set_real_price(option.id, 1.0, "test")

    ctx.now = datetime.combine(day, dtime(11, 0), tzinfo=IST)
    await evaluate(ctx)
    setup = ctx.state["setups"]["TESTCO"]
    assert setup["status"] == "exited"
    assert setup["exit_premium"] == 1.0
    assert setup["pnl"] == (1.0 - 5.0) * 500

    # ---- 3:10 report ----
    ctx.now = datetime.combine(day, dtime(15, 10), tzinfo=IST)
    await evaluate(ctx)
    assert ctx.state["report_sent"] is True
