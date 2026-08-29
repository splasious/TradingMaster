import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker import Broker, BrokerAccount, BrokerConnection, ConnectionStatus
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.live_trading.kill_switch import activate as activate_kill_switch
from app.services.live_trading.kill_switch import deactivate as deactivate_kill_switch
from app.services.live_trading.safety import check_live_trading_readiness
from app.services.strategy.state_machine import StrategyStatus


async def _setup(db_session: AsyncSession, *, strategy_status: str, connected: bool, risk_rules=None, position_sizing=None):
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"safety_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Safety User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code=f"delta_{uuid.uuid4().hex[:6]}", name="Delta Test", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(
        broker_account_id=broker_account.id,
        status=ConnectionStatus.CONNECTED.value if connected else ConnectionStatus.DISCONNECTED.value,
    ))

    strategy = Strategy(name="Safety Strategy", owner_id=user.id, code_type="python", status=strategy_status)
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[], parameters={},
        python_code='def generate_signal(c,p):\n    return "HOLD"',
        position_sizing=position_sizing if position_sizing is not None else {"type": "fixed_quantity", "value": 1},
        risk_rules=risk_rules if risk_rules is not None else {"stop_loss_pct": 5.0},
    )
    db_session.add(version)
    await db_session.commit()

    return {"strategy": strategy, "version": version, "broker_account": broker_account, "user": user}


async def test_fully_ready_strategy_passes(db_session: AsyncSession):
    ctx = await _setup(db_session, strategy_status=StrategyStatus.APPROVED.value, connected=True)
    result = await check_live_trading_readiness(db_session, ctx["strategy"], ctx["version"], ctx["broker_account"])
    assert result.passed is True
    assert result.failures == []


async def test_disconnected_broker_fails(db_session: AsyncSession):
    ctx = await _setup(db_session, strategy_status=StrategyStatus.APPROVED.value, connected=False)
    result = await check_live_trading_readiness(db_session, ctx["strategy"], ctx["version"], ctx["broker_account"])
    assert result.passed is False
    assert result.checks["broker_connected"] is False


async def test_unapproved_strategy_fails(db_session: AsyncSession):
    ctx = await _setup(db_session, strategy_status=StrategyStatus.PAPER_TRADING.value, connected=True)
    result = await check_live_trading_readiness(db_session, ctx["strategy"], ctx["version"], ctx["broker_account"])
    assert result.passed is False
    assert result.checks["strategy_approved"] is False


async def test_no_risk_limits_fails(db_session: AsyncSession):
    ctx = await _setup(db_session, strategy_status=StrategyStatus.APPROVED.value, connected=True, risk_rules={})
    result = await check_live_trading_readiness(db_session, ctx["strategy"], ctx["version"], ctx["broker_account"])
    assert result.passed is False
    assert result.checks["risk_limits_configured"] is False


async def test_kill_switch_active_fails_even_if_everything_else_ready(db_session: AsyncSession):
    ctx = await _setup(db_session, strategy_status=StrategyStatus.APPROVED.value, connected=True)
    await activate_kill_switch(db_session, ctx["user"].id, "manual test")
    await db_session.commit()

    result = await check_live_trading_readiness(db_session, ctx["strategy"], ctx["version"], ctx["broker_account"])
    assert result.passed is False
    assert result.checks["kill_switch_inactive"] is False
    assert "Kill switch" in result.failures[0] or any("kill switch" in f.lower() for f in result.failures)

    await deactivate_kill_switch(db_session)
    await db_session.commit()
    result2 = await check_live_trading_readiness(db_session, ctx["strategy"], ctx["version"], ctx["broker_account"])
    assert result2.checks["kill_switch_inactive"] is True
