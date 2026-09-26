"""Each user's strategies, and everything run from them, are private to that
user -- administrators included (services/ownership.py)."""

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.backfill_platform import BfWatchlist
from app.models.backtest import BacktestJob
from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LiveOrder
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.notifications.telegram import telegram_allowed
from app.services.paper_trading.native_runner import run_native_strategy

TRADER_PASSWORD = "TraderPass123!"


async def _headers(client: AsyncClient, email: str, password: str) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _users(db: AsyncSession) -> tuple[User, User]:
    admin = (await db.execute(select(User).where(User.email == "admin@tradingmaster.internal"))).scalar_one()
    role = (await db.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    trader = User(email="trader@tradingmaster.internal", hashed_password=hash_password(TRADER_PASSWORD), full_name="Trader")
    trader.user_roles = [UserRole(role=role)]
    db.add(trader)
    await db.flush()
    return admin, trader


async def _strategy(db: AsyncSession, owner: User, name: str, *, versions: int = 1, code: str | None = None) -> Strategy:
    strategy = Strategy(name=name, owner_id=owner.id, code_type="native" if code else "python")
    db.add(strategy)
    await db.flush()
    for n in range(1, versions + 1):
        db.add(StrategyVersion(
            strategy_id=strategy.id, version_number=n, timeframe="1d", instrument_ids=[], parameters={},
            python_code=code or f"# v{n}", position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
        ))
    await db.flush()
    return strategy


async def _instrument(db: AsyncSession) -> Instrument:
    instrument = Instrument(exchange="NSE", symbol="INFY", name="Infosys", instrument_type="equity", data_source="zerodha_kite", external_ref="INFY")
    db.add(instrument)
    await db.flush()
    return instrument


async def test_each_login_lists_only_its_own_strategies(client, seeded_admin, db_session):
    admin, trader = await _users(db_session)
    await _strategy(db_session, admin, "Admin strategy", versions=2)
    theirs = await _strategy(db_session, trader, "Trader strategy")
    await db_session.commit()
    admin_headers = await _headers(client, seeded_admin["email"], seeded_admin["password"])
    trader_headers = await _headers(client, trader.email, TRADER_PASSWORD)

    mine = (await client.get("/api/v1/strategies", headers=admin_headers)).json()
    assert [(s["name"], s["latest_version"]["version_number"]) for s in mine] == [("Admin strategy", 2)]
    assert [s["name"] for s in (await client.get("/api/v1/strategies", headers=trader_headers)).json()] == ["Trader strategy"]
    assert (await client.get(f"/api/v1/strategies/{theirs.id}", headers=admin_headers)).status_code == 403
    assert (await client.get(f"/api/v1/strategies/{theirs.id}", headers=trader_headers)).status_code == 200


async def test_backtest_results_are_private(client, seeded_admin, db_session):
    admin, trader = await _users(db_session)
    theirs = await _strategy(db_session, trader, "Trader strategy")
    version = (await db_session.execute(select(StrategyVersion).where(StrategyVersion.strategy_id == theirs.id))).scalar_one()
    job = BacktestJob(strategy_id=theirs.id, strategy_version_id=version.id, instrument_id=(await _instrument(db_session)).id, timeframe="1d")
    db_session.add(job)
    await db_session.commit()
    admin_headers = await _headers(client, seeded_admin["email"], seeded_admin["password"])
    trader_headers = await _headers(client, trader.email, TRADER_PASSWORD)

    for path in (f"/api/v1/backtests/{job.id}", f"/api/v1/backtests/{job.id}/result", f"/api/v1/backtests/{job.id}/trades",
                 f"/api/v1/backtests?strategy_id={theirs.id}", f"/api/v1/native-backtests?strategy_id={theirs.id}"):
        assert (await client.get(path, headers=admin_headers)).status_code == 404, path
    assert (await client.get(f"/api/v1/backtests/{job.id}", headers=trader_headers)).status_code == 200
    assert len((await client.get(f"/api/v1/backtests?strategy_id={theirs.id}", headers=trader_headers)).json()) == 1


async def test_live_deployments_orders_and_watchlists_are_private(client, seeded_admin, db_session):
    admin, trader = await _users(db_session)
    theirs = await _strategy(db_session, trader, "Trader strategy")
    version = (await db_session.execute(select(StrategyVersion).where(StrategyVersion.strategy_id == theirs.id))).scalar_one()
    broker = (await db_session.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one()
    account = BrokerAccount(user_id=trader.id, broker_id=broker.id, account_label="Trader", environment="live")
    db_session.add(account)
    await db_session.flush()
    deployment = LiveDeployment(
        owner_id=trader.id, strategy_id=theirs.id, strategy_version_id=version.id,
        instrument_id=(await _instrument(db_session)).id, broker_account_id=account.id,
    )
    db_session.add(deployment)
    await db_session.flush()
    db_session.add_all([
        LiveOrder(deployment_id=deployment.id, client_order_id=uuid.uuid4().hex, side="buy", quantity=1, status="filled"),
        BfWatchlist(owner_id=trader.id, name="Trader list"),
        BfWatchlist(owner_id=admin.id, name="Admin list"),
    ])
    await db_session.commit()
    admin_headers = await _headers(client, seeded_admin["email"], seeded_admin["password"])
    trader_headers = await _headers(client, trader.email, TRADER_PASSWORD)

    assert (await client.get("/api/v1/live-trading/deployments", headers=admin_headers)).json() == []
    assert len((await client.get("/api/v1/live-trading/deployments", headers=trader_headers)).json()) == 1
    assert (await client.get(f"/api/v1/live-trading/orders?deployment_id={deployment.id}", headers=admin_headers)).json() == []
    assert len((await client.get(f"/api/v1/live-trading/orders?deployment_id={deployment.id}", headers=trader_headers)).json()) == 1
    assert (await client.post(f"/api/v1/live-trading/deployments/{deployment.id}/stop", headers=admin_headers)).status_code == 403
    lists = (await client.get("/api/v1/backfill-platform/watchlists", headers=admin_headers)).json()
    assert [w["name"] for w in lists] == ["Admin list"]


async def _native_deployment(db: AsyncSession, owner: User, code: str) -> PaperNativeDeployment:
    strategy = await _strategy(db, owner, f"Native {owner.email}", code=code)
    version = (await db.execute(select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id))).scalar_one()
    portfolio = PaperPortfolio(user_id=owner.id, cash=100000.0, initial_capital=100000.0)
    db.add(portfolio)
    await db.flush()
    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, status=DeploymentStatus.ACTIVE.value,
    )
    db.add(deployment)
    await db.commit()
    return deployment


async def test_telegram_only_for_an_administrators_strategies(seeded_admin, db_session):
    admin, trader = await _users(db_session)
    code = (
        "from app.services.notifications.telegram import telegram_allowed\n"
        "async def evaluate(ctx):\n"
        "    ctx.state['telegram'] = telegram_allowed.get()\n"
    )
    theirs = await _native_deployment(db_session, trader, code)
    mine = await _native_deployment(db_session, admin, code)

    await run_native_strategy(db_session, theirs)
    await run_native_strategy(db_session, mine)

    assert theirs.state["telegram"] is False
    assert mine.state["telegram"] is True
    assert telegram_allowed.get() is True  # reset after each run
