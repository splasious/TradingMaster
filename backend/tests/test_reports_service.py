import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LiveTrade
from app.models.paper_trading import DeploymentStatus, PaperDeployment, PaperPortfolio, PaperTrade
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.reports.service import get_trade_rows, rows_to_csv, summarize


async def _setup(db_session: AsyncSession) -> dict:
    role = Role(name="trader", description="Trader")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"rpt_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Reports User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    instrument = Instrument(exchange="NSE", symbol="RPTX", name="Reports Co", instrument_type="equity", data_source="yahoo_nse", external_ref="RPTX")
    db_session.add(instrument)
    await db_session.flush()

    strategy = Strategy(name="Reports Strategy", owner_id=user.id, code_type="visual")
    db_session.add(strategy)
    await db_session.flush()

    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[str(instrument.id)],
        parameters={}, entry_rules={"all": []}, exit_rules={"all": []}, python_code=None,
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    portfolio = PaperPortfolio(user_id=user.id, cash=100000.0, initial_capital=100000.0)
    db_session.add(portfolio)
    await db_session.flush()

    paper_deployment = PaperDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        timeframe="1d", status=DeploymentStatus.ACTIVE.value,
    )
    db_session.add(paper_deployment)
    await db_session.flush()

    broker = Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()

    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Primary", environment="live")
    db_session.add(broker_account)
    await db_session.flush()

    live_deployment = LiveDeployment(
        owner_id=user.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        broker_account_id=broker_account.id, timeframe="1d", status="active",
    )
    db_session.add(live_deployment)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    db_session.add(
        PaperTrade(
            deployment_id=paper_deployment.id, entry_ts=now - timedelta(days=2), entry_price=100.0,
            exit_ts=now - timedelta(days=1), exit_price=110.0, quantity=10, pnl=100.0, pnl_pct=10.0, exit_reason="take_profit",
        )
    )
    db_session.add(
        LiveTrade(
            deployment_id=live_deployment.id, entry_ts=now - timedelta(hours=5), entry_price=200.0,
            exit_ts=now - timedelta(hours=1), exit_price=190.0, quantity=5, pnl=-50.0, pnl_pct=-5.0, exit_reason="stop_loss",
        )
    )
    await db_session.commit()

    return {"user": user}


async def test_get_trade_rows_merges_paper_and_live_and_sorts_by_exit(db_session: AsyncSession):
    ctx = await _setup(db_session)
    rows = await get_trade_rows(db_session, ctx["user"].id, None, None, None)

    assert len(rows) == 2
    assert rows[0].environment == "paper"
    assert rows[0].exit_reason == "take_profit"
    assert rows[1].environment == "live"
    assert rows[1].exit_reason == "stop_loss"
    assert rows[0].exit_ts < rows[1].exit_ts


async def test_get_trade_rows_filters_by_environment(db_session: AsyncSession):
    ctx = await _setup(db_session)
    paper_rows = await get_trade_rows(db_session, ctx["user"].id, "paper", None, None)
    assert len(paper_rows) == 1
    assert paper_rows[0].environment == "paper"

    live_rows = await get_trade_rows(db_session, ctx["user"].id, "live", None, None)
    assert len(live_rows) == 1
    assert live_rows[0].environment == "live"


async def test_rows_to_csv_includes_header_and_data(db_session: AsyncSession):
    ctx = await _setup(db_session)
    rows = await get_trade_rows(db_session, ctx["user"].id, None, None, None)
    csv_text = rows_to_csv(rows)

    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("environment,strategy,instrument,entry_ts")
    assert len(lines) == 3


async def test_summarize_computes_net_pnl_and_win_rate(db_session: AsyncSession):
    ctx = await _setup(db_session)
    rows = await get_trade_rows(db_session, ctx["user"].id, None, None, None)
    summary = summarize(rows)

    assert summary.trade_count == 2
    assert summary.net_pnl == 50.0
    assert summary.win_rate_pct == 50.0
    assert summary.best_trade == 100.0
    assert summary.worst_trade == -50.0


def test_summarize_handles_no_trades():
    summary = summarize([])
    assert summary.trade_count == 0
    assert summary.net_pnl == 0.0
    assert summary.win_rate_pct == 0.0
