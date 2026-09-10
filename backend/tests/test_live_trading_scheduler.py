from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment
from app.models.market_data import OhlcvCandle
from app.models.strategy import Strategy, StrategyVersion
from app.services.live_trading.scheduler import LiveTradingScheduler
from app.services.strategy.state_machine import StrategyStatus
from tests.test_live_trading_oms import ALWAYS_BUY, NEVER, FakeDeltaTransport, _setup


async def test_run_evaluates_active_deployments(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)

    scheduler = LiveTradingScheduler()
    evaluated = await scheduler.run(db_session)
    assert evaluated == 1
    assert len(fake.placed_orders) == 1  # the entry actually ran, not just counted


async def test_run_ignores_stopped_deployments(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    ctx["deployment"].status = "stopped"
    await db_session.commit()
    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)

    scheduler = LiveTradingScheduler()
    evaluated = await scheduler.run(db_session)
    assert evaluated == 0
    assert len(fake.placed_orders) == 0


async def test_run_one_deployment_failing_does_not_stop_the_others(db_session: AsyncSession, monkeypatch):
    ctx1 = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    # A second deployment reusing ctx1's user/broker_account (Broker.code
    # and Instrument(exchange, symbol) are both unique -- _setup can't be
    # called twice in one test) but its own instrument/strategy/version.
    instrument2 = Instrument(exchange="DELTA", symbol="LIVX2", name="Live Test Perp 2", instrument_type="perpetual_future", data_source="delta_exchange", external_ref="LIVX2")
    db_session.add(instrument2)
    await db_session.flush()
    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 100 + i
        db_session.add(OhlcvCandle(instrument_id=instrument2.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test"))
    strategy2 = Strategy(name="Live Strategy 2", owner_id=ctx1["user"].id, code_type="visual", status=StrategyStatus.APPROVED.value)
    db_session.add(strategy2)
    await db_session.flush()
    version2 = StrategyVersion(
        strategy_id=strategy2.id, version_number=1, timeframe="1d", instrument_ids=[str(instrument2.id)], parameters={},
        entry_rules=ALWAYS_BUY, exit_rules=NEVER, position_sizing={"type": "fixed_quantity", "value": 2}, risk_rules={"stop_loss_pct": 5.0},
    )
    db_session.add(version2)
    await db_session.flush()
    deployment2 = LiveDeployment(
        owner_id=ctx1["user"].id, strategy_id=strategy2.id, strategy_version_id=version2.id, instrument_id=instrument2.id,
        broker_account_id=ctx1["broker_account"].id, timeframe="1d", status="active",
    )
    db_session.add(deployment2)
    await db_session.commit()

    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)

    import app.services.live_trading.scheduler as scheduler_module

    real_evaluate = scheduler_module.evaluate_live_deployment
    calls = {"n": 0}

    async def flaky_evaluate(db, deployment):
        calls["n"] += 1
        if deployment.id == ctx1["deployment"].id:
            raise RuntimeError("boom")
        return await real_evaluate(db, deployment)

    monkeypatch.setattr(scheduler_module, "evaluate_live_deployment", flaky_evaluate)

    scheduler = LiveTradingScheduler()
    evaluated = await scheduler.run(db_session)
    assert calls["n"] == 2  # both attempted
    assert evaluated == 1  # only the one that didn't raise counted
    assert len(fake.placed_orders) == 1  # the second deployment's entry still went through
