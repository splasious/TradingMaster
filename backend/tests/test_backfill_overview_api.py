"""The Data Backfill page's API: overview, "saved up to" freshness, the
stocks table, schedule, "Top up now", retry, pause. Dates are relative to
the real last NSE session so the expectations hold on any day."""

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfCoverage, BfSettings, BfSymbol
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.user import User
from app.services.backfill_platform import overview
from app.services.backfill_platform.coverage import IST, last_completed_session, previous_trading_day
from app.services.backfill_platform.worker import backfill_worker


@pytest.fixture(autouse=True)
def _fresh_cache():
    overview.clear_cache()
    backfill_worker.paused = False
    yield
    overview.clear_cache()
    backfill_worker.paused = False


async def _headers(client: AsyncClient, admin: dict) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": admin["email"], "password": admin["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _at(d: date, h: int, m: int) -> datetime:
    return datetime.combine(d, time(h, m), tzinfo=IST)


async def _seed(db: AsyncSession) -> dict:
    session = last_completed_session(datetime.now(timezone.utc))
    two_back = previous_trading_day(previous_trading_day(session))
    db.add(BfSettings(id=1, coverage_built_at=datetime.now(timezone.utc)))
    current = BfSymbol(source="zerodha", symbol="CURRENTCO", display_name="Current Co")
    lagging = BfSymbol(source="zerodha", symbol="LAGGINGCO", display_name="Lagging Co")
    expired = BfSymbol(source="zerodha_nfo", symbol="OLDOPT", display_name="Old option", expiry=two_back)
    empty = BfSymbol(source="zerodha_nfo", symbol="NEVERTRADED", display_name="Never traded", expiry=session + timedelta(days=30))
    db.add_all([current, lagging, expired, empty])
    await db.flush()

    def cov(symbol, tf, last, bars_last_day=25):
        db.add(BfCoverage(symbol_id=symbol.id, timeframe=tf, first_ts=last - timedelta(days=60), last_ts=last, bar_count=1000, last_day_bars=bars_last_day))

    cov(current, "15m", _at(session, 15, 15))
    cov(current, "1d", _at(session, 0, 0), 1)
    cov(lagging, "15m", _at(two_back, 15, 15))  # 2 sessions behind
    cov(lagging, "1d", _at(session, 0, 0), 1)
    cov(expired, "15m", _at(two_back, 15, 15))  # expired: complete, not behind
    now = datetime.now(timezone.utc)
    interrupted = BfBackfillJob(symbol_id=current.id, source="zerodha", timeframe="5m", status=BfBackfillStatus.FAILED.value,
                                error_message="Interrupted by a server restart -- re-run this backfill if still needed.", completed_at=now)
    failed = BfBackfillJob(symbol_id=lagging.id, source="zerodha", timeframe="30m", status=BfBackfillStatus.FAILED.value,
                           error_message="Zerodha Kite API error 'NetworkException': upstream", completed_at=now)
    redone_failure = BfBackfillJob(symbol_id=lagging.id, source="zerodha", timeframe="60m", status=BfBackfillStatus.FAILED.value,
                                   error_message="x", completed_at=now - timedelta(hours=2), created_at=now - timedelta(hours=3))
    redone = BfBackfillJob(symbol_id=lagging.id, source="zerodha", timeframe="60m", status=BfBackfillStatus.COMPLETED.value,
                           completed_at=now - timedelta(hours=1), created_at=now - timedelta(hours=1))
    db.add_all([interrupted, failed, redone_failure, redone])
    await db.commit()
    return {"session": session, "two_back": two_back, "interrupted": interrupted, "failed": failed}


async def _connect_zerodha(db: AsyncSession, email: str) -> None:
    user = (await db.execute(select(User).where(User.email == email))).scalar_one()
    broker = (await db.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Kite", environment="paper")
    db.add(account)
    await db.flush()
    db.add(BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "access_token": "t"}))))
    db.add(BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTED.value))
    await db.commit()


async def test_overview_reports_saved_up_to_and_what_needs_attention(client, seeded_admin, db_session):
    seeded = await _seed(db_session)
    body = (await client.get("/api/v1/backfill-platform/overview", headers=await _headers(client, seeded_admin))).json()

    assert body["coverage_ready"] is True
    assert body["last_session"] == seeded["session"].isoformat()
    nse = {c["timeframe"]: c for c in body["segments"][0]["cells"]}
    assert (nse["1d"]["status"], nse["1d"]["current"], nse["1d"]["symbols"]) == ("ok", 2, 2)
    assert (nse["15m"]["status"], nse["15m"]["behind"], nse["15m"]["sessions_behind"]) == ("bad", 1, 2)
    assert "1m" not in nse  # 1-minute is no longer kept
    nfo15 = {c["timeframe"]: c for c in body["segments"][1]["cells"]}["15m"]
    assert (nfo15["status"], nfo15["symbols"], nfo15["expired"]) == ("ok", 0, 1)
    assert [seg["source"] for seg in body["segments"]] == ["zerodha", "zerodha_nfo"]  # Delta Exchange is hidden

    # The daily bars are current, 15m is not: headline = the daily's close, flagged.
    headline = body["headline"]
    assert headline["status"] == "bad" and headline["timeframes_current"] == 1
    assert headline["saved_up_to"].startswith(seeded["session"].isoformat())
    assert headline["behind"] == [{"timeframe": "15m", "sessions_behind": 2}]

    titles = [a["title"] for a in body["attention"]]
    assert titles[0] == "Zerodha is not logged in"
    assert "1 job stopped by a server restart" in titles and "1 job failed" in titles  # the redone failure isn't counted
    assert "1 NSE stock behind on 15m" in titles
    assert "1 NFO contract has no data yet" in titles
    empty = next(a for a in body["attention"] if a["title"] == "1 NFO contract has no data yet")
    assert empty["detail"] == "1 still active -- retried in each NFO top-up."
    assert body["queue"]["state"] == "idle" and body["schedule"]["topup_time"] == "16:15"


async def test_freshness_is_the_compact_nse_status(client, seeded_admin, db_session):
    await _seed(db_session)
    body = (await client.get("/api/v1/backfill-platform/freshness", headers=await _headers(client, seeded_admin))).json()
    assert set(body) >= {"headline", "timeframes", "live_today_until", "zerodha_login", "next_run_at", "delta_paused", "queue", "live_window"}
    assert body["live_window"] == {"start": "09:00", "end": "15:30"}
    assert [c["timeframe"] for c in body["timeframes"]] == ["5m", "15m", "30m", "60m", "1d"]
    assert body["zerodha_login"] == {"connected": False, "status": "not_set_up"} and body["delta_paused"] is True


async def test_stocks_table_filters_and_counts(client, seeded_admin, db_session):
    await _seed(db_session)
    headers = await _headers(client, seeded_admin)
    body = (await client.get("/api/v1/backfill-platform/coverage/stocks?status=behind", headers=headers)).json()
    assert body["counts"] == {"all": 2, "current": 1, "behind": 1, "partial": 0, "failed": 2}
    assert [r["symbol"] for r in body["rows"]] == ["LAGGINGCO"]
    assert body["rows"][0]["cells"]["15m"]["sessions_behind"] == 2
    searched = (await client.get("/api/v1/backfill-platform/coverage/stocks?q=curr", headers=headers)).json()
    assert [r["symbol"] for r in searched["rows"]] == ["CURRENTCO"]


async def test_schedule_is_admin_only_and_validated(client, seeded_admin, db_session):
    await _seed(db_session)
    headers = await _headers(client, seeded_admin)
    payload = {"auto_topup_zerodha": True, "auto_topup_zerodha_nfo": False, "delta_enabled": False,
               "topup_time": "18:05", "live_start": "09:05", "live_end": "15:30", "topup_timeframes": ["5m", "15m", "1d"]}
    resp = await client.put("/api/v1/backfill-platform/schedule", json=payload, headers=headers)
    assert resp.status_code == 200 and resp.json()["topup_time"] == "18:05" and resp.json()["auto_topup_zerodha_nfo"] is False
    assert (resp.json()["live_start"], resp.json()["live_end"]) == ("09:05", "15:30")
    bad_window = await client.put("/api/v1/backfill-platform/schedule", json=payload | {"live_start": "15:40"}, headers=headers)
    assert bad_window.status_code == 422 and "start before it ends" in bad_window.text
    assert (await client.put("/api/v1/backfill-platform/schedule", json=payload | {"topup_time": "25:00"}, headers=headers)).status_code == 422
    assert (await client.put("/api/v1/backfill-platform/schedule", json=payload | {"topup_timeframes": ["1wk"]}, headers=headers)).status_code == 422


async def test_top_up_now_needs_a_zerodha_login_then_queues_what_is_behind(client, seeded_admin, db_session):
    await _seed(db_session)
    headers = await _headers(client, seeded_admin)
    resp = await client.post("/api/v1/backfill-platform/topup", json={}, headers=headers)
    assert resp.status_code == 409 and "Log in to Zerodha" in resp.json()["detail"]

    await _connect_zerodha(db_session, seeded_admin["email"])
    resp = await client.post("/api/v1/backfill-platform/topup", json={"sources": ["zerodha"]}, headers=headers)
    assert resp.status_code == 202 and resp.json() == {"queued": {"zerodha": 1}}  # LAGGINGCO 15m
    cancelled = (await client.post("/api/v1/backfill-platform/runs/cancel", headers=headers)).json()
    assert cancelled == {"runs": 1, "jobs_cancelled": 1}
    # NFO: the expired contract is left alone; the active one with no data yet is tried again.
    resp = await client.post("/api/v1/backfill-platform/topup", json={"sources": ["zerodha_nfo"]}, headers=headers)
    assert resp.json() == {"queued": {"zerodha_nfo": 1}}

    overview_body = (await client.get("/api/v1/backfill-platform/overview", headers=headers)).json()
    assert overview_body["queue"]["state"] == "running" and overview_body["queue"]["run"]["total"] == 1

    cancelled = (await client.post("/api/v1/backfill-platform/runs/cancel", headers=headers)).json()
    assert cancelled == {"runs": 1, "jobs_cancelled": 1}


async def test_retry_failed_requeues_only_the_asked_kind(client, seeded_admin, db_session):
    seeded = await _seed(db_session)
    headers = await _headers(client, seeded_admin)
    resp = await client.post("/api/v1/backfill-platform/jobs/retry-failed", json={"kind": "interrupted"}, headers=headers)
    assert resp.json() == {"requeued": 1}
    for job, expected in ((seeded["interrupted"], "pending"), (seeded["failed"], "failed")):
        await db_session.refresh(job)
        assert job.status == expected


async def test_pause_and_resume_the_worker(client, seeded_admin):
    headers = await _headers(client, seeded_admin)
    assert (await client.post("/api/v1/backfill-platform/worker/pause", headers=headers)).json() == {"paused": True}
    assert backfill_worker.paused is True
    assert (await client.post("/api/v1/backfill-platform/worker/resume", headers=headers)).json() == {"paused": False}
