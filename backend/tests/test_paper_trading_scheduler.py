from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.paper_trading.engine import evaluate_deployment
from app.services.paper_trading.scheduler import diagnose_evaluation_freshness
from tests.test_paper_trading_engine import ALWAYS_BUY, NEVER, _setup


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
