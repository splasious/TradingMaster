"""The PCR Analysis tab's live row (services/options/pcr_live.py and
pcr_snapshots.live_reading, 26 Oct): a record's fields as they read right
now, against the latest record, never saved; one Kite reading shared by
every viewer for 15 seconds, and only while NSE trades."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.time import as_aware_utc
from app.models.pcr import PcrSnapshot, PcrStrikeOi
from app.services.options import pcr_live
from app.services.options import pcr_snapshots as ps
from tests.test_pcr_snapshots import (
    EXPIRIES,
    IST,
    FakeKite,
    _no_pauses,  # noqa: F401 -- the fixture: no pauses between quote calls
    at,
    capture,
)

SPOT = 24000.0


async def counts(db) -> tuple[int, int]:
    return (await db.scalar(select(func.count()).select_from(PcrSnapshot)),
            await db.scalar(select(func.count()).select_from(PcrStrikeOi)))


async def test_the_live_row_reads_against_the_latest_record_and_is_never_saved(db_session):
    kite = FakeKite(lambda t: SPOT)
    record = await capture(db_session, kite, at(25, 10, 0))
    record_ts, record_pcr = record.ts, record.pcr  # the reading rolls its session back: read these first
    saved = await counts(db_session)

    kite.now = at(25, 10, 20)  # one 15-minute step of OI growth since the 10:00 record
    row = await ps.live_reading(db_session, kite, "NIFTY", at(25, 10, 20))
    assert row["status"] == "live" and as_aware_utc(row["ts"]) == at(25, 10, 20) and row["prev_ts"] == record_ts
    per_side = 81 * 4  # ATM ±40 strikes of 4 expiries, each compared with the 10:00 record
    assert row["oi_change_contracts"] == 2 * per_side
    assert row["call_oi_change"] == pytest.approx(1_000 * per_side) and row["put_oi_change"] == pytest.approx(3_000 * per_side)
    assert row["oi_change_pcr"] == pytest.approx(3.0) and row["positioning"] == "bullish"
    assert row["pcr"] is not None and row["prev_pcr"] == record_pcr and len(row["expiry_rows"]) == 4
    assert await counts(db_session) == saved  # nothing kept


@pytest.fixture
def live(db_engine, monkeypatch):
    """pcr_live on the test database, with a fake Kite whose reads are counted."""
    kite = FakeKite(lambda t: SPOT)
    monkeypatch.setattr(pcr_live, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))
    reads = []

    async def broker(db):
        reads.append(1)
        return kite

    monkeypatch.setattr(pcr_live, "kite_broker", broker)
    monkeypatch.setattr(pcr_live, "_cache", {})
    return kite, reads


async def test_one_reading_serves_every_viewer_for_15_seconds(db_session, live):
    kite, reads = live
    await capture(db_session, kite, at(25, 10, 0))
    kite.now = at(25, 10, 5)
    first, why = await pcr_live.live_row("NIFTY", at(25, 10, 5))
    assert why is None and first["status"] == "live"
    again, _ = await pcr_live.live_row("NIFTY", at(25, 10, 5) + timedelta(seconds=14))
    assert again is first and len(reads) == 1
    later, _ = await pcr_live.live_row("NIFTY", at(25, 10, 5) + timedelta(seconds=15))
    assert later is not first and len(reads) == 2


async def test_no_live_row_outside_the_session_or_without_a_zerodha_login(db_session, live, monkeypatch):
    _, reads = live
    before_open, why = await pcr_live.live_row("NIFTY", datetime(2026, 9, 25, 9, 14, tzinfo=IST).astimezone(timezone.utc))
    assert before_open is None and why == pcr_live.CLOSED
    saturday, why = await pcr_live.live_row("NIFTY", datetime(2026, 9, 26, 11, 0, tzinfo=IST).astimezone(timezone.utc))
    assert saturday is None and why == pcr_live.CLOSED and reads == []

    async def logged_out(db):
        return None

    monkeypatch.setattr(pcr_live, "kite_broker", logged_out)
    row, why = await pcr_live.live_row("NIFTY", at(25, 11, 0))
    assert row is None and why == pcr_live.NOT_LOGGED_IN


async def test_a_kite_failure_says_so_and_saves_nothing(db_session, live):
    kite, _ = live
    await capture(db_session, kite, at(25, 10, 0))
    saved = await counts(db_session)

    async def broken(instruments):
        raise ps.KiteAPIError("quote failed")

    kite.get_quote_batch = broken
    kite.now = at(25, 10, 7)
    row, why = await pcr_live.live_row("NIFTY", at(25, 10, 7))
    assert row is None and why == pcr_live.KITE_FAILED and await counts(db_session) == saved


async def test_the_endpoint_returns_the_live_row(client, seeded_admin, monkeypatch):
    resp = await client.post("/api/v1/auth/login", json={"email": seeded_admin["email"], "password": seeded_admin["password"]})
    auth_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    row = {"ts": at(25, 10, 7), "session_date": at(25, 10, 7).astimezone(IST).date(), "status": "live", "pcr": 1.1, "expiries": EXPIRIES[:4]}

    async def fake_live_row(underlying, now=None):
        return row, None

    monkeypatch.setattr("app.api.v1.endpoints.options.live_row", fake_live_row)
    out = (await client.get("/api/v1/options/pcr/live?underlying=NIFTY", headers=auth_headers)).json()
    assert out["row"]["status"] == "live" and out["row"]["pcr"] == 1.1 and out["refresh_seconds"] == 15 and out["reason"] is None
    assert (await client.get("/api/v1/options/pcr/live?underlying=BANKNIFTY", headers=auth_headers)).status_code == 404
