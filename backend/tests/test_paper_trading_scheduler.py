from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_trading import PaperPosition
from app.services.paper_trading.engine import evaluate_deployment
from app.services.paper_trading.scheduler import PaperTradingScheduler, diagnose_evaluation_freshness
from tests.test_paper_trading_engine import ALWAYS_BUY, NEVER, _setup

# A real, known NSE trading Thursday, well inside market hours (09:15-15:30
# IST == 03:45-10:00 UTC) -- same reference point test_market_data_freshness.py
# uses.
MARKET_OPEN_NOW = datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc)
MARKET_CLOSED_NOW = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)  # a Saturday


async def test_diagnose_evaluation_freshness_reports_never_evaluated(db_session: AsyncSession):
    await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    diag = await diagnose_evaluation_freshness(db_session)
    assert diag["active_deployments"] == 1
    assert diag["never_evaluated"] == 1
    assert diag["oldest_evaluation_age_seconds"] is None
    assert diag["newest_evaluation_age_seconds"] is None


async def test_diagnose_evaluation_freshness_reports_age_after_evaluation(db_session: AsyncSession):
    ctx = await _setup(db_session, entry_rules=NEVER, exit_rules=NEVER)  # NEVER/NEVER -- stays flat, just a hold
    await evaluate_deployment(db_session, ctx["deployment"])

    diag = await diagnose_evaluation_freshness(db_session)
    assert diag["active_deployments"] == 1
    assert diag["never_evaluated"] == 0
    assert diag["oldest_evaluation_age_seconds"] is not None
    assert diag["oldest_evaluation_age_seconds"] < 5  # just evaluated, must be near-zero
    assert diag["newest_evaluation_age_seconds"] == diag["oldest_evaluation_age_seconds"]


async def test_tick_once_evaluates_active_deployments_during_market_hours(db_session: AsyncSession):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)

    scheduler = PaperTradingScheduler()
    evaluated = await scheduler.tick_once(db_session, now=MARKET_OPEN_NOW)
    assert evaluated == 1

    position = (
        await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))
    ).scalar_one_or_none()
    assert position is not None


async def test_tick_once_is_a_noop_when_market_closed(db_session: AsyncSession):
    """The whole point of this gate: a native or paper strategy must never
    open, hold, or flat-close a position on a day NSE never opened -- see
    the incident this closes: a native strategy trading a whole Saturday
    against Friday's frozen price for an artifactual 0.00 P&L, because
    nothing here previously had any idea the exchange was shut."""
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)

    scheduler = PaperTradingScheduler()
    evaluated = await scheduler.tick_once(db_session, now=MARKET_CLOSED_NOW)
    assert evaluated == 0

    position = (
        await db_session.execute(select(PaperPosition).where(PaperPosition.deployment_id == ctx["deployment"].id))
    ).scalar_one_or_none()
    assert position is None
    assert ctx["deployment"].last_evaluated_at is None  # never touched


async def test_diagnose_evaluation_freshness_counts_a_skipped_evaluation_too(db_session: AsyncSession, monkeypatch):
    """The whole point of this diagnostic: a deployment stuck skipping
    (stale data) must still show up as "recently evaluated", not blend
    in with deployments the scheduler has never reached at all -- this is
    exactly the ambiguity that made "is auto-evaluation actually running"
    unanswerable before today's last_evaluated_at fix."""
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)

    import app.services.paper_trading.engine as engine_module
    monkeypatch.setattr(engine_module, "check_freshness", lambda candles, timeframe, now: "stale")

    outcome = await evaluate_deployment(db_session, ctx["deployment"])
    assert outcome.action == "skipped"

    diag = await diagnose_evaluation_freshness(db_session)
    assert diag["never_evaluated"] == 0
    assert diag["oldest_evaluation_age_seconds"] < 5
