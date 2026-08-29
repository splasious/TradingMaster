import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment
from app.models.paper_trading import DeploymentStatus, PaperDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _create_strategy(client: AsyncClient, headers: dict, name: str) -> dict:
    resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": name,
            "version": {
                "entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]},
                "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]},
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()


async def test_delete_strategy_with_no_deployments_succeeds(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy = await _create_strategy(client, headers, "Deletable Strategy")

    resp = await client.delete(f"/api/v1/strategies/{strategy['id']}", headers=headers)
    assert resp.status_code == 204

    get_resp = await client.get(f"/api/v1/strategies/{strategy['id']}", headers=headers)
    assert get_resp.status_code == 404


async def test_delete_strategy_removes_backtest_artifacts(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy = await _create_strategy(client, headers, "Strategy With Backtests")

    from app.models.backtest import BacktestJob, BacktestResult

    version_id = strategy["latest_version"]["id"]
    instrument = Instrument(exchange="NSE", symbol="DELX", name="Delete Test Co", instrument_type="equity", data_source="yahoo_nse", external_ref="DELX")
    db_session.add(instrument)
    await db_session.flush()
    job = BacktestJob(strategy_id=uuid.UUID(strategy["id"]), strategy_version_id=uuid.UUID(version_id), instrument_id=instrument.id, timeframe="1d", status="completed")
    db_session.add(job)
    await db_session.flush()
    db_session.add(BacktestResult(job_id=job.id, metrics={"net_profit": 1.0}, equity_curve=[]))
    await db_session.commit()

    resp = await client.delete(f"/api/v1/strategies/{strategy['id']}", headers=headers)
    assert resp.status_code == 204

    remaining_jobs = (await db_session.execute(select(BacktestJob).where(BacktestJob.strategy_id == uuid.UUID(strategy["id"])))).scalars().all()
    assert remaining_jobs == []


async def test_delete_strategy_with_paper_deployment_is_refused(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy = await _create_strategy(client, headers, "Strategy With Paper Deployment")

    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    version_id = strategy["latest_version"]["id"]
    instrument = Instrument(exchange="NSE", symbol="DELY", name="Delete Test Co 2", instrument_type="equity", data_source="yahoo_nse", external_ref="DELY")
    db_session.add(instrument)
    await db_session.flush()
    portfolio = PaperPortfolio(user_id=admin.id, cash=100000.0, initial_capital=100000.0)
    db_session.add(portfolio)
    await db_session.flush()
    db_session.add(
        PaperDeployment(
            portfolio_id=portfolio.id, strategy_id=uuid.UUID(strategy["id"]), strategy_version_id=uuid.UUID(version_id),
            instrument_id=instrument.id, timeframe="1d", status=DeploymentStatus.ACTIVE.value,
        )
    )
    await db_session.commit()

    resp = await client.delete(f"/api/v1/strategies/{strategy['id']}", headers=headers)
    assert resp.status_code == 409
    assert "paper or live deployment" in resp.json()["detail"]

    get_resp = await client.get(f"/api/v1/strategies/{strategy['id']}", headers=headers)
    assert get_resp.status_code == 200


async def test_delete_strategy_with_live_deployment_is_refused(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy = await _create_strategy(client, headers, "Strategy With Live Deployment")

    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    version_id = strategy["latest_version"]["id"]
    instrument = Instrument(exchange="DELTA", symbol="DELZ", name="Delete Test Perp", instrument_type="perpetual_future", data_source="delta_exchange", external_ref="DELZ")
    db_session.add(instrument)
    await db_session.flush()
    broker = (await db_session.execute(select(Broker).where(Broker.code == "delta_exchange"))).scalar_one()
    broker_account = BrokerAccount(user_id=admin.id, broker_id=broker.id, account_label="Live Test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(
        LiveDeployment(
            owner_id=admin.id, strategy_id=uuid.UUID(strategy["id"]), strategy_version_id=uuid.UUID(version_id),
            instrument_id=instrument.id, broker_account_id=broker_account.id, timeframe="1d", status="active",
        )
    )
    await db_session.commit()

    resp = await client.delete(f"/api/v1/strategies/{strategy['id']}", headers=headers)
    assert resp.status_code == 409


async def test_delete_strategy_requires_ownership(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy = await _create_strategy(client, headers, "Someone Else's Strategy")

    role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    other = User(email="other_delete@tradingmaster.internal", hashed_password=hash_password("OtherPass123!"), full_name="Other")
    other.user_roles = [UserRole(role=role)]
    db_session.add(other)
    await db_session.commit()

    other_token = await _login(client, "other_delete@tradingmaster.internal", "OtherPass123!")
    resp = await client.delete(f"/api/v1/strategies/{strategy['id']}", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 403


async def test_delete_unknown_strategy_returns_404(client: AsyncClient, seeded_admin: dict):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    resp = await client.delete(f"/api/v1/strategies/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
