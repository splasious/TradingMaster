"""A native strategy's state must survive to the next tick exactly as the
strategy left it -- including changes made *inside* nested dicts, like
popping a stock out of state["holdings"]. The runner used to hand the
strategy a shallow copy: an in-place change to the nested holdings dict
also changed SQLAlchemy's own "before" snapshot of the JSON column, so it
saw nothing to save. Every tick then reloaded the old holdings and sold
the same stocks again -- one duplicate trade and cash credit every ~10s."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.paper_trading.native_runner import run_native_strategy

SELL_ONE_CODE = (
    "async def evaluate(ctx):\n"
    "    holdings = ctx.state.get('holdings', {})\n"
    "    if 'NYKAA' in holdings:\n"
    "        holdings.pop('NYKAA')\n"
    "        ctx.portfolio.cash += 100\n"
    "    holdings['SBIN'] = {'quantity': 1}\n"
    "    ctx.state['holdings'] = holdings\n"
    "    ctx.note('hold')\n"
)


async def test_nested_state_changes_are_saved_between_ticks(db_engine, db_session: AsyncSession):
    role = Role(name=f"state_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"state_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="State")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()
    strategy = Strategy(name="State Test", owner_id=user.id, code_type="native")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, python_code=SELL_ONE_CODE)
    portfolio = PaperPortfolio(user_id=user.id, cash=1000.0, initial_capital=1000.0)
    db_session.add_all([version, portfolio])
    await db_session.flush()
    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
        status=DeploymentStatus.ACTIVE.value,
        state={"seeded": True, "holdings": {"NYKAA": {"quantity": 11}, "HINDALCO": {"quantity": 4}}},
    )
    db_session.add(deployment)
    await db_session.commit()
    deployment_id = deployment.id

    # Each tick in its own session, as the scheduler does -- state is
    # whatever the database holds, not what's left in memory.
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    for _ in range(3):
        async with sessions() as db:
            await run_native_strategy(db, await db.get(PaperNativeDeployment, deployment_id))

    async with sessions() as db:
        saved = await db.get(PaperNativeDeployment, deployment_id)
        cash = (await db.get(PaperPortfolio, saved.portfolio_id)).cash
    assert saved.state["holdings"] == {"HINDALCO": {"quantity": 4}, "SBIN": {"quantity": 1}}
    assert cash == 1100.0  # sold once -- not again on every later tick
