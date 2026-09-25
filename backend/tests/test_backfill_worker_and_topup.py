"""The durable queue (worker.py), freshness in NSE sessions (coverage.py)
and the daily automatic top-up (topup.py)."""

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.encryption import encrypt_payload
from app.models.backfill_platform import (
    JOB_PRIORITY_MANUAL,
    JOB_PRIORITY_SCHEDULED,
    BfBackfillJob,
    BfBackfillRun,
    BfBackfillStatus,
    BfCoverage,
    BfSettings,
    BfSymbol,
)
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.services.backfill_platform import coverage, jobs, topup, worker
from app.services.backfill_platform.coverage import last_completed_session, sessions_behind

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=IST)


@pytest.fixture
def sessions(db_engine, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    for module in (jobs, topup, coverage):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    return factory


# -------------------------------------------------------------- freshness --

def test_the_last_completed_session_rolls_over_at_the_close_and_skips_weekends():
    assert last_completed_session(ist(2026, 9, 25, 12, 0)) == date(2026, 9, 24)  # Fri, market open
    assert last_completed_session(ist(2026, 9, 25, 15, 30)) == date(2026, 9, 25)  # Fri, closed
    assert last_completed_session(ist(2026, 9, 27, 10, 0)) == date(2026, 9, 25)  # Sun
    assert last_completed_session(ist(2026, 9, 28, 9, 0)) == date(2026, 9, 25)  # Mon, before the open


def test_sessions_behind_counts_missing_closed_sessions():
    friday_noon, friday_evening = ist(2026, 9, 25, 12, 0), ist(2026, 9, 25, 16, 0)
    thursday_last_15m = ist(2026, 9, 24, 15, 15)
    assert sessions_behind(thursday_last_15m, "15m", friday_noon) == 0
    assert sessions_behind(thursday_last_15m, "15m", friday_evening) == 1
    assert sessions_behind(ist(2026, 9, 21, 15, 29), "1m", friday_noon) == 3  # Mon's in; Tue-Thu missing
    # A day saved only up to 11:00 is not complete -- it counts as missing.
    assert sessions_behind(ist(2026, 9, 24, 11, 0), "1m", friday_noon) == 1
    # Kite dates a daily candle at midnight; it covers the whole session.
    assert sessions_behind(ist(2026, 9, 24), "1d", friday_noon) == 0
    # Friday's close is in: nothing is missing over the weekend.
    assert sessions_behind(ist(2026, 9, 25, 15, 15), "15m", ist(2026, 9, 27, 10, 0)) == 0


# ----------------------------------------------------------------- worker --

async def _symbol(db: AsyncSession, name: str, source: str = "zerodha", expiry: date | None = None) -> BfSymbol:
    symbol = BfSymbol(source=source, symbol=name, display_name=name, expiry=expiry)
    db.add(symbol)
    await db.flush()
    return symbol


async def test_the_worker_takes_manual_jobs_first_and_waits_out_retry_delays(db_session, sessions):
    symbol = await _symbol(db_session, "QUEUED")
    scheduled = BfBackfillJob(symbol_id=symbol.id, source="zerodha", timeframe="15m", priority=JOB_PRIORITY_SCHEDULED)
    manual = BfBackfillJob(symbol_id=symbol.id, source="zerodha", timeframe="1d", priority=JOB_PRIORITY_MANUAL)
    delayed = BfBackfillJob(
        symbol_id=symbol.id, source="zerodha", timeframe="5m", priority=JOB_PRIORITY_MANUAL,
        run_after=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add_all([scheduled, manual, delayed])
    await db_session.commit()

    assert await worker.next_job_id() == manual.id  # the delayed one is waiting out its back-off
    manual.status = BfBackfillStatus.COMPLETED.value
    await db_session.commit()
    assert await worker.next_job_id() == scheduled.id
    assert await worker.next_job_id(ignore_delays=True) == delayed.id


# ----------------------------------------------------------------- top-up --

async def _connected_account(db: AsyncSession) -> uuid.UUID:
    from app.models.user import User

    user = User(email=f"topup_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Topup")
    db.add(user)
    await db.flush()
    broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
    db.add(broker)
    await db.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Kite", environment="paper")
    db.add(account)
    await db.flush()
    db.add(BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "access_token": "t"}))))
    db.add(BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTED.value))
    await db.commit()
    return user.id


async def _tracked(db: AsyncSession, symbol: BfSymbol, timeframe: str, last_ts: datetime) -> None:
    db.add(BfCoverage(symbol_id=symbol.id, timeframe=timeframe, first_ts=last_ts - timedelta(days=30), last_ts=last_ts, bar_count=100, last_day_bars=25))


async def _topup_fixture(db: AsyncSession) -> dict:
    db.add(BfSettings(id=1, coverage_built_at=datetime.now(timezone.utc)))
    behind = await _symbol(db, "BEHIND")
    current = await _symbol(db, "CURRENT")
    expired = await _symbol(db, "NIFTY26922C", "zerodha_nfo", expiry=date(2026, 9, 22))
    live_option = await _symbol(db, "NIFTY26929C", "zerodha_nfo", expiry=date(2026, 9, 29))
    never_traded = await _symbol(db, "NEVERTRADED", "zerodha_nfo", expiry=date(2026, 9, 29))  # active, no bars saved yet
    await _symbol(db, "EXPIREDEMPTY", "zerodha_nfo", expiry=date(2026, 9, 22))  # expired, no bars: Kite has none
    await _tracked(db, behind, "15m", ist(2026, 9, 21, 15, 15))
    await _tracked(db, behind, "1d", ist(2026, 9, 24))  # Thursday's daily: current until Friday's close
    await _tracked(db, current, "15m", ist(2026, 9, 25, 15, 15))
    await _tracked(db, expired, "15m", ist(2026, 9, 22, 15, 15))
    await _tracked(db, live_option, "5m", ist(2026, 9, 24, 15, 25))
    await db.commit()
    return {"behind": behind, "live_option": live_option, "never_traded": never_traded}


async def test_the_daily_topup_queues_only_what_is_behind(db_session, sessions):
    symbols = await _topup_fixture(db_session)
    user_id = await _connected_account(db_session)
    scheduler = topup.BackfillTopupScheduler()

    await scheduler.tick(ist(2026, 9, 25, 16, 0))  # before 16:15: nothing yet
    assert (await db_session.execute(select(BfBackfillRun))).scalars().all() == []

    await scheduler.tick(ist(2026, 9, 25, 16, 16))
    await scheduler.tick(ist(2026, 9, 25, 16, 17))  # a second tick doesn't start another run

    runs = {r.source: r for r in (await db_session.execute(select(BfBackfillRun))).scalars().all()}
    assert set(runs) == {"zerodha", "zerodha_nfo"}
    assert all(r.kind == "scheduled" and r.session_date == date(2026, 9, 25) and r.status == "running" for r in runs.values())
    queued = (await db_session.execute(select(BfBackfillJob))).scalars().all()
    got = {(j.symbol_id, j.timeframe, j.start_date, j.end_date, j.requested_by, j.priority) for j in queued}
    assert got == {
        # daily first (priority order), each from its last saved day to the session
        (symbols["behind"].id, "1d", date(2026, 9, 24), date(2026, 9, 25), user_id, JOB_PRIORITY_SCHEDULED + 0),
        (symbols["behind"].id, "15m", date(2026, 9, 21), date(2026, 9, 25), user_id, JOB_PRIORITY_SCHEDULED + 3),
        # NFO after every NSE timeframe
        (symbols["live_option"].id, "5m", date(2026, 9, 24), date(2026, 9, 25), user_id, JOB_PRIORITY_SCHEDULED + 6 + 4),
        # an active contract with nothing saved yet is tried again, whole default history
        (symbols["never_traded"].id, "15m", None, date(2026, 9, 25), user_id, JOB_PRIORITY_SCHEDULED + 6 + 3),
    }
    assert (runs["zerodha"].jobs_total, runs["zerodha_nfo"].jobs_total) == (2, 2)


async def test_the_topup_waits_for_a_zerodha_login_then_runs(db_session, sessions):
    await _topup_fixture(db_session)
    scheduler = topup.BackfillTopupScheduler()

    await scheduler.tick(ist(2026, 9, 25, 16, 16))
    runs = (await db_session.execute(select(BfBackfillRun))).scalars().all()
    assert {(r.status, r.message) for r in runs} == {("waiting_login", "Waiting for Zerodha login")}
    assert (await db_session.execute(select(BfBackfillJob))).scalars().all() == []

    await _connected_account(db_session)
    await scheduler.tick(ist(2026, 9, 25, 18, 0))  # logged in later: the waiting runs start
    runs = (await db_session.execute(select(BfBackfillRun))).scalars().all()
    for run in runs:
        await db_session.refresh(run)
    assert {r.status for r in runs} == {"running"} and len(runs) == 2
    assert len((await db_session.execute(select(BfBackfillJob))).scalars().all()) == 4


async def test_a_run_completes_when_its_jobs_are_done(db_session, sessions):
    await _topup_fixture(db_session)
    await _connected_account(db_session)
    scheduler = topup.BackfillTopupScheduler()
    await scheduler.tick(ist(2026, 9, 25, 16, 16))

    for job in (await db_session.execute(select(BfBackfillJob))).scalars().all():
        job.status = BfBackfillStatus.FAILED.value if job.source == "zerodha_nfo" else BfBackfillStatus.COMPLETED.value
    await db_session.commit()
    await scheduler.tick(ist(2026, 9, 25, 16, 30))

    runs = {r.source: r for r in (await db_session.execute(select(BfBackfillRun))).scalars().all()}
    for run in runs.values():
        await db_session.refresh(run)
    assert {r.status for r in runs.values()} == {"completed"}
    assert (runs["zerodha"].message, runs["zerodha_nfo"].message) == ("2 done", "0 done, 2 failed")


async def test_nothing_runs_on_a_segment_switched_off(db_session, sessions):
    await _topup_fixture(db_session)
    await _connected_account(db_session)
    settings = await db_session.get(BfSettings, 1)
    settings.auto_topup_zerodha_nfo = False
    await db_session.commit()

    await topup.BackfillTopupScheduler().tick(ist(2026, 9, 25, 16, 16))

    assert {r.source for r in (await db_session.execute(select(BfBackfillRun))).scalars().all()} == {"zerodha"}


async def test_a_topup_takes_over_jobs_an_earlier_one_still_has_queued(db_session, sessions):
    symbols = await _topup_fixture(db_session)
    await _connected_account(db_session)
    scheduler = topup.BackfillTopupScheduler()
    await scheduler.tick(ist(2026, 9, 24, 16, 16))  # Thursday's run: BEHIND 15m, NEVERTRADED 15m
    thursday = {r.source: r for r in (await db_session.execute(select(BfBackfillRun))).scalars().all()}
    assert (thursday["zerodha"].jobs_total, thursday["zerodha_nfo"].jobs_total) == (1, 1)

    await scheduler.tick(ist(2026, 9, 25, 16, 16))  # still queued at Friday's 16:15

    jobs = (await db_session.execute(select(BfBackfillJob))).scalars().all()
    assert len(jobs) == 4 and len({(j.symbol_id, j.timeframe) for j in jobs}) == 4  # no second job for a pair
    runs = {(r.session_date, r.source): r for r in (await db_session.execute(select(BfBackfillRun))).scalars().all()}
    for run in runs.values():
        await db_session.refresh(run)
    friday = {s: runs[(date(2026, 9, 25), s)] for s in ("zerodha", "zerodha_nfo")}
    carried = {j.symbol_id: j for j in jobs if j.symbol_id in (symbols["behind"].id, symbols["never_traded"].id) and j.timeframe == "15m"}
    for job in carried.values():
        await db_session.refresh(job)
        assert job.run_id == friday[job.source].id and job.end_date == date(2026, 9, 25)
    assert (friday["zerodha"].jobs_total, friday["zerodha_nfo"].jobs_total) == (2, 2)

    await scheduler.tick(ist(2026, 9, 25, 16, 17))  # the emptied Thursday runs close
    for source in ("zerodha", "zerodha_nfo"):
        run = runs[(date(2026, 9, 24), source)]
        await db_session.refresh(run)
        assert (run.status, run.jobs_total, run.message) == ("completed", 0, "1 queued job moved to the 25 Sep top-up")


async def test_a_waiting_run_is_superseded_by_the_next_sessions(db_session, sessions):
    await _topup_fixture(db_session)
    scheduler = topup.BackfillTopupScheduler()
    await scheduler.tick(ist(2026, 9, 24, 16, 16))  # Thursday, no login

    await scheduler.tick(ist(2026, 9, 25, 16, 16))  # Friday's run replaces it

    runs = sorted((await db_session.execute(select(BfBackfillRun))).scalars().all(), key=lambda r: (r.session_date, r.source))
    for run in runs:
        await db_session.refresh(run)
    assert [(r.session_date, r.status) for r in runs] == [
        (date(2026, 9, 24), "skipped"), (date(2026, 9, 24), "skipped"),
        (date(2026, 9, 25), "waiting_login"), (date(2026, 9, 25), "waiting_login"),
    ]


async def test_coverage_is_built_once_from_the_stored_bars(db_session, sessions):
    from app.models.backfill_platform import BfOhlcvBar

    symbol = await _symbol(db_session, "BUILT")
    for minutes in range(0, 25 * 15, 15):  # a full Thursday session of 15m bars
        db_session.add(BfOhlcvBar(symbol_id=symbol.id, timeframe="15m", ts=(ist(2026, 9, 24, 9, 15) + timedelta(minutes=minutes)).astimezone(timezone.utc),
                                  open=1, high=1, low=1, close=1))
    await db_session.commit()

    assert await coverage.build_coverage() == 1
    assert await coverage.build_coverage() == 0  # done: not rebuilt at every start

    row = await db_session.get(BfCoverage, (symbol.id, "15m"))
    assert (row.bar_count, row.last_day_bars) == (25, 25)
    assert row.last_ts.replace(tzinfo=timezone.utc) == ist(2026, 9, 24, 15, 15)


async def _admin(db: AsyncSession):
    from app.models.user import Role, User, UserRole

    role = Role(name="administrator", description="Administrator")
    db.add(role)
    await db.flush()
    admin = User(email="admin_topup@tradingmaster.internal", hashed_password="x", full_name="Admin")
    admin.user_roles = [UserRole(role=role)]
    db.add(admin)
    await db.commit()
    return admin


async def test_admins_are_reminded_to_log_in_to_zerodha_from_0845(db_session, sessions):
    from app.models.alert import Alert

    await _topup_fixture(db_session)
    admin = await _admin(db_session)
    scheduler = topup.BackfillTopupScheduler()

    await scheduler.tick(ist(2026, 9, 25, 8, 30))  # before 08:45 -- still time to log in
    assert (await db_session.execute(select(Alert))).scalars().all() == []

    await scheduler.tick(ist(2026, 9, 25, 8, 46))
    await scheduler.tick(ist(2026, 9, 25, 9, 30))  # once a day, not every minute
    alerts = (await db_session.execute(select(Alert))).scalars().all()
    assert [(a.user_id, a.title, a.severity) for a in alerts] == [(admin.id, "Log in to Zerodha", "warning")]

    await scheduler.tick(ist(2026, 9, 26, 9, 0))  # Saturday: no reminder
    assert len((await db_session.execute(select(Alert))).scalars().all()) == 1


async def test_no_reminder_when_logged_in(db_session, sessions):
    from app.models.alert import Alert

    await _topup_fixture(db_session)
    await _admin(db_session)
    await _connected_account(db_session)
    await topup.BackfillTopupScheduler().tick(ist(2026, 9, 25, 9, 0))
    assert (await db_session.execute(select(Alert))).scalars().all() == []


async def test_a_topup_with_failures_alerts_the_admins(db_session, sessions):
    from app.models.alert import Alert

    await _topup_fixture(db_session)
    await _admin(db_session)
    await _connected_account(db_session)
    scheduler = topup.BackfillTopupScheduler()
    await scheduler.tick(ist(2026, 9, 25, 16, 16))
    for job in (await db_session.execute(select(BfBackfillJob))).scalars().all():
        job.status = BfBackfillStatus.FAILED.value if job.source == "zerodha_nfo" else BfBackfillStatus.COMPLETED.value
    await db_session.commit()

    await scheduler.tick(ist(2026, 9, 25, 16, 40))

    titles = [a.title for a in (await db_session.execute(select(Alert))).scalars().all()]
    assert titles == ["NFO top-up finished with 2 failed jobs"]  # the clean NSE run raises none


def test_the_live_window_runs_0900_to_1530_on_trading_days():
    from app.services.backfill_platform.coverage import live_window_open

    assert not live_window_open(ist(2026, 9, 25, 8, 59))
    assert live_window_open(ist(2026, 9, 25, 9, 0))  # before the 09:15 open
    assert live_window_open(ist(2026, 9, 25, 15, 34))  # a few minutes past 15:30 for the closing candles
    assert not live_window_open(ist(2026, 9, 25, 15, 36))
    assert not live_window_open(ist(2026, 9, 26, 10, 0))  # Saturday
    assert not live_window_open(ist(2026, 10, 2, 10, 0))  # NSE holiday
    assert live_window_open(ist(2026, 9, 25, 9, 20), start="09:30") is False  # the page's setting is honoured
