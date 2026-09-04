import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperDeployment, PaperOrder, PaperPortfolio, PaperPosition, PaperTrade
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.engine import evaluate_deployment


async def _setup(
    db_session: AsyncSession, *, entry_rules=None, exit_rules=None, python_code=None, risk_rules=None, cash=100000.0
):
    role = Role(name="trader_pt", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"pt_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="PT User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    instrument = Instrument(exchange="NSE", symbol="PTX", name="Paper Co", instrument_type="equity", data_source="yahoo_nse", external_ref="PTX")
    db_session.add(instrument)
    await db_session.flush()

    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 100 + i
        db_session.add(
            OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test")
        )

    strategy = Strategy(name="PT Strategy", owner_id=user.id, code_type="python" if python_code else "visual")
    db_session.add(strategy)
    await db_session.flush()

    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[str(instrument.id)],
        parameters={}, entry_rules=entry_rules, exit_rules=exit_rules, python_code=python_code,
        position_sizing={"type": "fixed_quantity", "value": 10}, risk_rules=risk_rules or {},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=cash, initial_capital=cash)
    db_session.add(portfolio)
    await db_session.flush()

    deployment = PaperDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        timeframe="1d", status=DeploymentStatus.ACTIVE.value,
    )
    db_session.add(deployment)
    await db_session.commit()

    return {"instrument": instrument, "portfolio": portfolio, "deployment": deployment}


ALWAYS_BUY = {"all": [{"field": "close", "operator": ">", "value": 0}]}
NEVER = {"all": [{"field": "close", "operator": "<", "value": 0}]}


async def test_entry_signal_opens_position_and_debits_cash(db_session: AsyncSession):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    tick_engine._last_price.pop(ctx["instrument"].id, None)  # force fallback to candle close

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "entered"
    assert outcome.signal == "BUY"

    position = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))).scalar_one()
    assert position.quantity == 10

    await db_session.refresh(ctx["portfolio"])
    assert ctx["portfolio"].cash < 100000.0

    order = (await db_session.execute(select(PaperOrder).where(PaperOrder.deployment_id == ctx["deployment"].id))).scalar_one()
    assert order.status == "filled"
    assert order.side == "buy"


async def test_exit_signal_closes_position_and_records_trade(db_session: AsyncSession):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    tick_engine._last_price.pop(ctx["instrument"].id, None)
    await evaluate_deployment(db_session, ctx["deployment"])  # enters

    # flip rules: now exit condition is always true
    version = await db_session.get(StrategyVersion, ctx["deployment"].strategy_version_id)
    version.exit_rules = ALWAYS_BUY
    version.entry_rules = NEVER
    await db_session.commit()

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "exited"

    remaining_position = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))).scalar_one_or_none()
    assert remaining_position is None

    trade = (await db_session.execute(select(PaperTrade).where(PaperTrade.deployment_id == ctx["deployment"].id))).scalar_one()
    assert trade.quantity == 10


async def test_stop_loss_triggers_before_signal_check(db_session: AsyncSession):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER, risk_rules={"stop_loss_pct": 5.0})
    tick_engine._last_price.pop(ctx["instrument"].id, None)
    await evaluate_deployment(db_session, ctx["deployment"])  # enters at last candle close (129)

    position = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))).scalar_one()
    stop_price = position.avg_entry_price * 0.95
    tick_engine._last_price[ctx["instrument"].id] = stop_price - 1  # breach the stop

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "exited"
    assert outcome.reason == "stop_loss"


async def test_risk_engine_rejects_when_insufficient_cash(db_session: AsyncSession):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER, cash=1.0)
    tick_engine._last_price.pop(ctx["instrument"].id, None)

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "rejected"
    assert "Insufficient cash" in outcome.reason

    position = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))).scalar_one_or_none()
    assert position is None

    # Rejections leave no order record -- Orders/Trades reflects real
    # activity only; the audit log is the compliance trail for the attempt.
    order = (await db_session.execute(select(PaperOrder).where(PaperOrder.deployment_id == ctx["deployment"].id))).scalar_one_or_none()
    assert order is None


async def test_python_strategy_evaluates_via_sandbox(db_session: AsyncSession):
    code = 'def generate_signal(candles, params):\n    return "BUY" if candles[-1]["close"] > candles[0]["close"] else "HOLD"'
    ctx = await _setup(db_session, python_code=code)
    tick_engine._last_price.pop(ctx["instrument"].id, None)

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "entered"


async def test_indicator_based_rule_does_not_crash_on_mixed_tz_datetimes(db_session: AsyncSession):
    """Regression test: the synthetic "current bar" built from a live tick
    is a freshly constructed timezone-aware datetime, while candles loaded
    from SQLite come back naive (see core/time.py). An indicator-based rule
    (unlike a raw "close" rule) sends both through candles_to_frame's
    pandas sort, which used to crash with "can't compare offset-naive and
    offset-aware datetimes" -- see indicators/base.py's as_aware_utc fix."""
    ctx = await _setup(
        db_session,
        entry_rules={"all": [{"field": "rsi.rsi", "operator": ">", "value": 0}]},
        exit_rules=NEVER,
    )
    tick_engine._last_price[ctx["instrument"].id] = 150.0  # force the tz-aware synthetic-bar path

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action in ("entered", "hold")  # must not raise


async def test_basket_strategy_injects_rank_params_into_sandbox(db_session: AsyncSession):
    """A strategy attached to more than one instrument gets its rank within
    that basket (trailing momentum) injected as extra numeric params --
    the actual mechanism that makes a "top-N rotation" strategy possible
    despite the sandbox only ever seeing one instrument's own candles."""
    from app.services.paper_trading import ranking as ranking_module

    ranking_module._cache.clear()

    role = Role(name="trader_basket", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"basket_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Basket User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    n = 25
    base = datetime.now(timezone.utc) - timedelta(days=n)

    async def make_instrument(symbol: str, step: float) -> Instrument:
        instrument = Instrument(
            exchange="DELTA", symbol=symbol, name=symbol, instrument_type="perpetual_future",
            data_source="delta_exchange", external_ref=symbol,
        )
        db_session.add(instrument)
        await db_session.flush()
        for i in range(n):
            close = 100 + i * step
            db_session.add(
                OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close, high=close + 1, low=close - 1, close=close, volume=1000, source="test")
            )
        return instrument

    strong = await make_instrument("BASKETSTRONGUSD", step=3.0)
    weak = await make_instrument("BASKETWEAKUSD", step=-1.0)
    await db_session.commit()

    code = 'def generate_signal(candles, params):\n    return "BUY" if params.get("in_top_n", 0.0) >= 1.0 else "HOLD"'
    strategy = Strategy(name="Basket Strategy", owner_id=user.id, code_type="python")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d",
        instrument_ids=[str(strong.id), str(weak.id)], parameters={"top_n": 1},
        entry_rules=None, exit_rules=None, python_code=code,
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=100000.0, initial_capital=100000.0)
    db_session.add(portfolio)
    await db_session.flush()

    strong_deployment = PaperDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=strong.id,
        timeframe="1d", status=DeploymentStatus.ACTIVE.value,
    )
    weak_deployment = PaperDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=weak.id,
        timeframe="1d", status=DeploymentStatus.ACTIVE.value,
    )
    db_session.add_all([strong_deployment, weak_deployment])
    await db_session.commit()

    tick_engine._last_price.pop(strong.id, None)
    tick_engine._last_price.pop(weak.id, None)

    strong_outcome = await evaluate_deployment(db_session, strong_deployment)
    weak_outcome = await evaluate_deployment(db_session, weak_deployment)

    assert strong_outcome.action == "entered"  # top_n=1 -> only the strongest-momentum instrument is in_top_n
    assert weak_outcome.action == "hold"


async def test_no_price_data_skips_evaluation(db_session: AsyncSession):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    tick_engine._last_price.pop(ctx["instrument"].id, None)
    # delete all candles so there's truly no price source
    candles = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == ctx["instrument"].id))).scalars().all()
    for c in candles:
        await db_session.delete(c)
    await db_session.commit()

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "skipped"
