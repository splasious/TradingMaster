import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.broker import Broker
from app.models.user import Role, User, UserRole
from app.services.backtest import runner as backtest_runner
from app.services.backtest import optimization_runner
from app.services.market_data import backfill as market_data_backfill

@pytest_asyncio.fixture
async def db_engine():
    db_path = f"./test_{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    import os

    if os.path.exists(db_path):
        os.remove(db_path)


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_engine, monkeypatch) -> AsyncGenerator[AsyncClient, None]:
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # BackgroundTasks (backfill, backtests) run after the response is sent
    # and open their own DB session via a direct `from app.db.session import
    # AsyncSessionLocal` -- that's a *different* binding than the get_db
    # override above, so without this they'd silently operate against the
    # real dev database instead of this test's isolated one.
    monkeypatch.setattr(market_data_backfill, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(backtest_runner, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(optimization_runner, "AsyncSessionLocal", session_factory)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def seeded_admin(db_session: AsyncSession) -> dict:
    admin_role = Role(name="administrator", description="Administrator")
    trader_role = Role(name="trader", description="Trader")
    analyst_role = Role(name="analyst", description="Analyst")
    viewer_role = Role(name="viewer", description="Viewer")
    db_session.add_all([admin_role, trader_role, analyst_role, viewer_role])
    await db_session.flush()

    db_session.add_all(
        [
            Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True),
            Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True),
        ]
    )

    password = "AdminPass123!"
    admin = User(email="admin@tradingmaster.internal", hashed_password=hash_password(password), full_name="Admin")
    admin.user_roles = [UserRole(role=admin_role)]
    db_session.add(admin)
    await db_session.commit()

    return {"email": admin.email, "password": password}
