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
    db_session: AsyncSession, *, entry_rules=None, exit_rules=None, python_code=None, risk_rules=None, cash=100000.0,
    timeframe: str = "1d",
):
    role = Role(name="trader_pt", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"pt_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="PT User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    instrument = Instrument(exchange="NSE", symbol="PTX", name="Paper Co", instrument_type="equity", data_source="zerodha_kite", external_ref="PTX")
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
        strategy_id=strategy.id, version_number=1, timeframe=timeframe, instrument_ids=[str(instrument.id)],
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
        timeframe=timeframe, status=DeploymentStatus.ACTIVE.value,
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

    await db_session.refresh(ctx["deployment"])
    assert ctx["deployment"].last_signal == "BUY"  # the outcome's signal, not the "entered" action word


async def test_last_signal_persists_the_action_word_when_no_signal_is_computed(db_session: AsyncSession):
    """A "hold" outcome carries a real signal (HOLD/BUY/SELL) -- that's
    what gets stamped. But some outcomes (e.g. an outright config error)
    never compute a signal at all, so last_signal falls back to the
    outcome's own action word -- either way, this column must never sit
    blank while last_evaluated_at is advancing (see the wrapper's own
    docstring in engine.py)."""
    ctx = await _setup(db_session, entry_rules=NEVER, exit_rules=NEVER)
    tick_engine._last_price.pop(ctx["instrument"].id, None)

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "hold"
    assert outcome.signal == "HOLD"

    await db_session.refresh(ctx["deployment"])
    assert ctx["deployment"].last_signal == "HOLD"


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


async def test_exit_signal_ignores_live_tick_price_between_closed_candles(db_session: AsyncSession):
    """The actual production bug this test guards against: a live tick
    price satisfying the exit rule must NOT close the position on its own
    -- only a genuinely new closed candle can flip the signal, exactly
    like backtesting's candles[:i+1] convention (signals.py). Before this
    fix, evaluate_deployment injected a synthetic "current bar" built from
    the live tick, so an exit rule like "close > 129" (false against the
    real last candle, close=129) fired the instant the tick price moved
    above 129 -- causing real deployments to exit within minutes instead
    of holding through a full candle, confirmed live on "RS Scalper -
    Delta" (trades closing in 3-12 minutes on a 15m-timeframe strategy)."""
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    tick_engine._last_price.pop(ctx["instrument"].id, None)
    enter_outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert enter_outcome.action == "entered"

    # Last stored candle's close is 129 (see _setup: close = 100 + i, i up
    # to 29) -- this rule is false against that candle but would have been
    # true against the old synthetic bar once the tick below is set.
    version = await db_session.get(StrategyVersion, ctx["deployment"].strategy_version_id)
    version.exit_rules = {"all": [{"field": "close", "operator": ">", "value": 129}]}
    await db_session.commit()
    tick_engine._last_price[ctx["instrument"].id] = 150.0  # well above 129

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "hold"

    still_open = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))).scalar_one_or_none()
    assert still_open is not None


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
    """Regression test for indicators/base.py's as_aware_utc fix: an
    indicator-based rule (unlike a raw "close" rule) sends candle
    timestamps through candles_to_frame's pandas sort, which used to crash
    with "can't compare offset-naive and offset-aware datetimes" whenever
    the series mixed naive and aware datetimes. evaluate_deployment no
    longer builds a synthetic "current bar" from the live tick (see
    evaluate_deployment's docstring on why -- it made an entry/exit rule
    re-evaluate against constantly-moving live price every scheduler tick
    instead of once per closed bar), so this no longer exercises that
    exact mixed-tz path, but stays as general coverage that indicator
    rules evaluate cleanly against real stored candles."""
    ctx = await _setup(
        db_session,
        entry_rules={"all": [{"field": "rsi.rsi", "operator": ">", "value": 0}]},
        exit_rules=NEVER,
    )
    tick_engine._last_price[ctx["instrument"].id] = 150.0

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


async def test_basket_strategy_injects_advance_decline_ratio_into_sandbox(db_session: AsyncSession):
    """Basket-wide breadth (advancers/decliners among the strategy's own
    instrument_ids) reaches every deployment's sandbox call as the same
    shared value, unlike rank/in_top_n which differ per instrument -- this
    is the real, computable substitute for a PCR-style basket sentiment
    gauge on instruments (Delta's RWA tokens) that have no options market
    to derive an actual put/call ratio from."""
    from app.services.paper_trading import ranking as ranking_module

    ranking_module._cache.clear()

    role = Role(name="trader_breadth", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"breadth_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Breadth User")
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

    # 2 advancers, 1 decliner -> advance_decline_ratio == 2.0 for the whole basket.
    up_a = await make_instrument("BREADTHUPAUSD", step=1.0)
    up_b = await make_instrument("BREADTHUPBUSD", step=2.0)
    down = await make_instrument("BREADTHDOWNUSD", step=-1.0)
    await db_session.commit()

    code = 'def generate_signal(candles, params):\n    return "BUY" if params.get("advance_decline_ratio", 0.0) >= 1.5 else "HOLD"'
    strategy = Strategy(name="Breadth Strategy", owner_id=user.id, code_type="python")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d",
        instrument_ids=[str(up_a.id), str(up_b.id), str(down.id)], parameters={},
        entry_rules=None, exit_rules=None, python_code=code,
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=100000.0, initial_capital=100000.0)
    db_session.add(portfolio)
    await db_session.flush()

    deployments = {}
    for inst in (up_a, up_b, down):
        deployment = PaperDeployment(
            portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=inst.id,
            timeframe="1d", status=DeploymentStatus.ACTIVE.value,
        )
        db_session.add(deployment)
        deployments[inst.symbol] = deployment
    await db_session.commit()

    for inst in (up_a, up_b, down):
        tick_engine._last_price.pop(inst.id, None)

    # The basket-wide ratio (2.0) is the same for every instrument -- even
    # the decliner's own deployment sees it and enters, since this is a
    # market-breadth filter, not a per-instrument momentum check.
    for symbol, deployment in deployments.items():
        outcome = await evaluate_deployment(db_session, deployment)
        assert outcome.action == "entered", f"{symbol}: expected entry on basket-wide breadth, got {outcome.action} ({outcome.reason})"


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


async def test_percent_capital_sizing_uses_pool_equity_not_shrinking_cash(db_session: AsyncSession):
    """percent_capital sizing must size off the whole pool's current net
    worth (cash + mark-to-market of already-open positions), matching the
    portfolio backtest engine's equity_now (portfolio_engine.py's
    per-candidate `allocation = min(equity_now * pct/100, cash)`) -- not
    off remaining cash alone. Sizing off cash alone made every successive
    fill in the same burst smaller than the last even though net worth
    hadn't shrunk, just moved from cash into stock."""
    role = Role(name="trader_sizing", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"sizing_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Sizing User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    n = 30
    base = datetime.now(timezone.utc) - timedelta(days=n)

    async def make_instrument(symbol: str) -> Instrument:
        instrument = Instrument(
            exchange="DELTA", symbol=symbol, name=symbol, instrument_type="perpetual_future",
            data_source="delta_exchange", external_ref=symbol,
        )
        db_session.add(instrument)
        await db_session.flush()
        for i in range(n):
            db_session.add(
                OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=99.5, high=101, low=99, close=100, volume=1000, source="test")
            )
        return instrument

    first = await make_instrument("SIZEFIRSTUSD")
    second = await make_instrument("SIZESECONDUSD")
    await db_session.commit()

    strategy = Strategy(name="Sizing Strategy", owner_id=user.id, code_type="visual")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[str(first.id), str(second.id)],
        parameters={}, entry_rules=ALWAYS_BUY, exit_rules=NEVER, python_code=None,
        position_sizing={"type": "percent_capital", "value": 20.0}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=100000.0, initial_capital=100000.0)
    db_session.add(portfolio)
    await db_session.flush()

    first_deployment = PaperDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=first.id,
        timeframe="1d", status=DeploymentStatus.ACTIVE.value,
    )
    second_deployment = PaperDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=second.id,
        timeframe="1d", status=DeploymentStatus.ACTIVE.value,
    )
    db_session.add_all([first_deployment, second_deployment])
    await db_session.commit()

    tick_engine._last_price.pop(first.id, None)
    tick_engine._last_price.pop(second.id, None)

    first_outcome = await evaluate_deployment(db_session, first_deployment)
    assert first_outcome.action == "entered"
    first_position = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == first_deployment.id))).scalar_one()
    assert first_position.quantity == 200.0  # 20% of 100,000 pool equity / 100 price

    second_outcome = await evaluate_deployment(db_session, second_deployment)
    assert second_outcome.action == "entered"
    second_position = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == second_deployment.id))).scalar_one()
    # Pool equity is still ~100,000 (cash 80,000 + first position's 20,000
    # mark-to-market) even though cash alone dropped to 80,000 -- sizing off
    # cash alone would give floor(80,000 * 0.20 / 100) = 160 here instead.
    assert second_position.quantity == 200.0


async def test_1wk_deployment_derives_signal_from_stored_daily_candles(db_session: AsyncSession):
    """Kite has no native weekly interval -- a "1wk" deployment must not
    just silently see zero candles forever. load_candles' resample
    fallback derives real weekly bars from the "1d" candles _setup seeds,
    the same underlying data active_timeframe_sync_scheduler.py now keeps
    fresh for a "1wk"/"1mo" pair (see that module's own comment)."""
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER, timeframe="1wk")
    tick_engine._last_price.pop(ctx["instrument"].id, None)

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "entered"
    assert outcome.signal == "BUY"


async def test_stale_candle_data_skips_and_raises_data_disconnected_alert(db_session: AsyncSession, monkeypatch):
    """check_freshness itself is unit-tested directly in
    test_market_data_freshness.py -- this only confirms evaluate_deployment
    actually calls it and reacts correctly (skip, don't silently trade;
    alert once, not every tick). Mocked at the check_freshness boundary
    rather than depending on real wall-clock NSE market hours."""
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    tick_engine._last_price.pop(ctx["instrument"].id, None)

    import app.services.paper_trading.engine as engine_module
    monkeypatch.setattr(engine_module, "check_freshness", lambda candles, timeframe, now: "latest candle is way too old")

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "skipped"
    assert outcome.reason == "latest candle is way too old"

    position = (await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))).scalar_one_or_none()
    assert position is None  # never traded on the stale data

    from app.models.alert import Alert, AlertType
    alerts = (await db_session.execute(select(Alert).where(Alert.alert_type == AlertType.DATA_DISCONNECTED.value))).scalars().all()
    assert len(alerts) == 1

    await db_session.refresh(ctx["deployment"])
    assert ctx["deployment"].last_evaluated_at is not None  # a skipped tick still counts as "the scheduler reached this deployment"
    assert ctx["deployment"].last_signal == "SKIPPED"  # no BUY/SELL/HOLD was computed -- falls back to the action word
    assert ctx["deployment"].last_signal_reason == "latest candle is way too old"


async def test_stale_data_alert_is_throttled_by_cooldown(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    tick_engine._last_price.pop(ctx["instrument"].id, None)

    import app.services.paper_trading.engine as engine_module
    monkeypatch.setattr(engine_module, "check_freshness", lambda candles, timeframe, now: "stale")

    from app.core.time import as_aware_utc

    await evaluate_deployment(db_session, ctx["deployment"])
    await db_session.refresh(ctx["deployment"])
    first_evaluated_at = as_aware_utc(ctx["deployment"].last_evaluated_at)
    await evaluate_deployment(db_session, ctx["deployment"])  # second tick, same cooldown window

    from app.models.alert import Alert, AlertType
    alerts = (await db_session.execute(select(Alert).where(Alert.alert_type == AlertType.DATA_DISCONNECTED.value))).scalars().all()
    assert len(alerts) == 1  # throttled, not duplicated per tick

    await db_session.refresh(ctx["deployment"])
    # last_evaluated_at must still advance on the second tick even though
    # the alert itself was throttled -- a deployment repeatedly hitting
    # this branch must not look frozen/unreached in the UI.
    second_evaluated_at = as_aware_utc(ctx["deployment"].last_evaluated_at)
    assert second_evaluated_at is not None
    assert second_evaluated_at >= first_evaluated_at
