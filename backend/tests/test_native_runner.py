import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.paper_trading.native_runner import run_native_strategy


async def _setup(db_session: AsyncSession, python_code: str, *, cash: float = 100000.0):
    role = Role(name=f"native_pt_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"native_pt_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Native PT User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    strategy = Strategy(name="Native Test Strategy", owner_id=user.id, code_type="native")
    db_session.add(strategy)
    await db_session.flush()

    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[], parameters={},
        entry_rules=None, exit_rules=None, python_code=python_code,
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
    await db_session.commit()

    return {"portfolio": portfolio, "deployment": deployment}


async def test_run_native_strategy_calls_evaluate_and_persists_state(db_session: AsyncSession):
    code = (
        "async def evaluate(ctx):\n"
        "    ctx.state['ticks'] = ctx.state.get('ticks', 0) + 1\n"
        "    ctx.note('hold', signal='NOOP', reason=f\"tick {ctx.state['ticks']}\")\n"
    )
    ctx = await _setup(db_session, code)
    outcome = await run_native_strategy(db_session, ctx["deployment"])
    assert outcome.action == "hold"
    assert outcome.signal == "NOOP"
    assert outcome.reason == "tick 1"

    await db_session.refresh(ctx["deployment"])
    assert ctx["deployment"].state == {"ticks": 1}
    assert ctx["deployment"].last_signal == "NOOP"

    outcome2 = await run_native_strategy(db_session, ctx["deployment"])
    assert outcome2.reason == "tick 2"


async def test_run_native_strategy_reports_missing_evaluate_function(db_session: AsyncSession):
    ctx = await _setup(db_session, "x = 1\n")
    outcome = await run_native_strategy(db_session, ctx["deployment"])
    assert outcome.action == "error"
    assert "must define async def evaluate" in outcome.reason


async def test_run_native_strategy_reports_syntax_errors_without_crashing(db_session: AsyncSession):
    ctx = await _setup(db_session, "def evaluate(:\n  pass")
    outcome = await run_native_strategy(db_session, ctx["deployment"])
    assert outcome.action == "error"
    assert "SyntaxError" in outcome.reason


async def test_run_native_strategy_isolates_a_runtime_error_inside_evaluate(db_session: AsyncSession):
    code = "async def evaluate(ctx):\n    raise ValueError('boom')\n"
    ctx = await _setup(db_session, code)
    outcome = await run_native_strategy(db_session, ctx["deployment"])
    assert outcome.action == "error"
    assert "boom" in outcome.reason
    # last_evaluated_at must still have advanced -- a buggy strategy must
    # not look indistinguishable from "the scheduler never reached it".
    await db_session.refresh(ctx["deployment"])
    assert ctx["deployment"].last_evaluated_at is not None


async def test_run_native_strategy_gives_evaluate_real_imports_unlike_the_sandbox(db_session: AsyncSession):
    """The whole point of "native": real stdlib imports work, unlike
    RestrictedPython's blocked __import__."""
    code = (
        "import math\n"
        "async def evaluate(ctx):\n"
        "    ctx.note('hold', reason=str(math.floor(3.7)))\n"
    )
    ctx = await _setup(db_session, code)
    outcome = await run_native_strategy(db_session, ctx["deployment"])
    assert outcome.reason == "3"


async def test_run_native_strategy_can_move_portfolio_cash_via_open_close_leg(db_session: AsyncSession):
    from app.models.instrument import Instrument

    instrument = Instrument(
        exchange="NSE", symbol="NATIVEOPT", name="Native Opt", instrument_type="option",
        data_source="zerodha_kite", external_ref="NATIVEOPT", strike=100.0, option_type="CE",
        expiry=(datetime.now(timezone.utc) + timedelta(days=7)).date(), lot_size=50,
    )
    db_session.add(instrument)
    await db_session.commit()

    code = (
        "from sqlalchemy import select\n"
        "from app.models.instrument import Instrument\n"
        "async def evaluate(ctx):\n"
        f"    inst = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == 'NATIVEOPT'))).scalar_one()\n"
        "    await ctx.open_leg(inst, 'sell', 50.0, 20.0)\n"
        "    ctx.note('entered', signal='SHORT')\n"
    )
    ctx = await _setup(db_session, code)
    starting_cash = ctx["portfolio"].cash
    outcome = await run_native_strategy(db_session, ctx["deployment"])
    assert outcome.action == "entered"

    await db_session.refresh(ctx["portfolio"])
    assert ctx["portfolio"].cash == starting_cash + 50.0 * 20.0
