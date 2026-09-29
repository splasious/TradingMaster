"""Backfill speed-up: jobs reuse one logged-in Kite session and its open
connection, every Kite request queues on the app-wide limit of 3 a second,
and the worker runs a few jobs at a time without two taking the same one."""

import asyncio
import uuid
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfSymbol
from app.services.backfill_platform import jobs, worker
from app.services.broker import zerodha_broker
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker

END = date(2026, 9, 25)
EXPIRED = KiteAPIError("Zerodha Kite API error 'TokenException': Incorrect `api_key` or `access_token`.")


class FakeKite:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.kept_open = 0

    def keep_connection_open(self) -> None:
        self.kept_open += 1

    async def get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return []


@pytest.fixture
def sessions(db_engine, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(jobs, "AsyncSessionLocal", factory)
    return factory


def kite_logins(monkeypatch, *brokers: FakeKite) -> list:
    """Each login hands out the next fake; returns the list of logins made."""
    made: list = []

    async def login(db, user_id):
        broker = brokers[min(len(made), len(brokers) - 1)]
        made.append(broker)
        return broker

    monkeypatch.setattr(jobs, "get_authenticated_kite_broker", login)
    return made


async def _jobs(sessions, count: int, days: int = 5, timeframe: str = "15m") -> list[uuid.UUID]:
    async with sessions() as db:
        ids = []
        for n in range(count):
            symbol = BfSymbol(source="zerodha", symbol=f"S{n}", display_name=f"S{n}")
            db.add(symbol)
            await db.flush()
            job = BfBackfillJob(symbol_id=symbol.id, source="zerodha", timeframe=timeframe,
                                start_date=END - timedelta(days=days), end_date=END)
            db.add(job)
            await db.flush()
            ids.append(job.id)
        await db.commit()
        return ids


async def _status(sessions, job_id) -> str:
    async with sessions() as db:
        return (await db.get(BfBackfillJob, job_id)).status


async def test_jobs_reuse_one_kite_login_and_its_connection(sessions, monkeypatch):
    kite = FakeKite()
    logins = kite_logins(monkeypatch, kite)
    ids = await _jobs(sessions, 3)

    assert await worker.drain_queue() == 3

    assert [await _status(sessions, i) for i in ids] == [BfBackfillStatus.COMPLETED.value] * 3
    assert logins == [kite] and kite.kept_open == 1 and kite.calls == 3


async def test_an_expired_session_logs_in_again_once(sessions, monkeypatch):
    stale, fresh = FakeKite(error=EXPIRED), FakeKite()
    logins = kite_logins(monkeypatch, stale, fresh)
    [job_id] = await _jobs(sessions, 1)

    await worker.drain_queue()

    assert await _status(sessions, job_id) == BfBackfillStatus.COMPLETED.value
    assert logins == [stale, fresh] and jobs._kite_brokers  # the fresh session is kept for the next job


async def test_a_login_kite_keeps_rejecting_fails_the_job_without_looping(sessions, monkeypatch):
    logins = kite_logins(monkeypatch, FakeKite(error=EXPIRED))
    [job_id] = await _jobs(sessions, 1)

    await worker.drain_queue()

    assert await _status(sessions, job_id) == BfBackfillStatus.FAILED.value
    assert len(logins) == 2


async def test_every_kite_request_waits_on_the_shared_limit(sessions, monkeypatch):
    kite_logins(monkeypatch, kite := FakeKite())
    waits = []

    async def wait():
        waits.append(1)

    monkeypatch.setattr(jobs.history_pacer, "wait", wait)
    await _jobs(sessions, 1, days=250, timeframe="5m")  # Kite takes 99 days of 5m a request: three requests

    await worker.drain_queue()

    assert kite.calls == 3 and len(waits) == 3


async def test_the_worker_runs_jobs_at_once_each_only_once(sessions, monkeypatch):
    ids = await _jobs(sessions, 7)
    ran: list[uuid.UUID] = []
    at_once = {"now": 0, "most": 0}

    async def run_job(job_id):
        at_once["now"] += 1
        at_once["most"] = max(at_once["most"], at_once["now"])
        ran.append(job_id)
        await asyncio.sleep(0.05)  # waiting on Kite
        async with sessions() as db:
            await db.execute(update(BfBackfillJob).where(BfBackfillJob.id == job_id).values(status=BfBackfillStatus.COMPLETED.value))
            await db.commit()
        at_once["now"] -= 1

    monkeypatch.setattr(jobs, "run_bf_backfill_job", run_job)
    slots = worker.BackfillWorker(parallel=3)
    slots.start()
    try:
        for _ in range(100):
            if len(ran) == len(ids) and at_once["now"] == 0:
                break
            await asyncio.sleep(0.05)
    finally:
        slots.stop()

    assert sorted(ran) == sorted(ids)  # every job, none twice
    assert at_once["most"] == 3


def test_parallel_downloads_stay_between_one_and_five(monkeypatch):
    from app.core.config import get_settings

    assert worker.BackfillWorker(parallel=0).parallel == 1
    assert worker.BackfillWorker(parallel=9).parallel == worker.MAX_PARALLEL
    monkeypatch.setattr(get_settings(), "backfill_parallel", 1)
    assert worker.BackfillWorker().parallel == 1  # BACKFILL_PARALLEL=1: one after another, as before


def _broker(handler) -> ZerodhaKiteBroker:
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    broker.keep_connection_open()
    broker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return broker


async def test_a_kept_open_connection_serves_every_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "success", "data": {"user_id": "AB1234"}})

    broker = _broker(handler)
    assert await broker.get_profile() == {"user_id": "AB1234"}
    assert await broker.get_profile() == {"user_id": "AB1234"}
    assert seen == ["/user/profile", "/user/profile"]
    await broker.aclose()
    assert broker._client is None


async def test_a_read_on_a_connection_kite_closed_is_asked_once_more_a_write_never():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"status": "success", "data": {"ok": True}})

    broker = _broker(handler)
    assert await broker._request("GET", "/user/profile") == {"ok": True}
    assert calls == ["GET", "GET"]

    calls.clear()
    with pytest.raises(httpx.RemoteProtocolError):
        await broker._request("POST", "/orders/regular", data={"x": "1"})
    assert calls == ["POST"]


def test_the_contract_lookup_is_built_once_per_instrument_list():
    rows = [{"tradingsymbol": "NIFTY26O0622800CE", "instrument_token": "1"}, {"tradingsymbol": "", "instrument_token": "2"}]
    first = zerodha_broker._symbol_index("NFO", rows)
    assert first == {"NIFTY26O0622800CE": rows[0]}
    assert zerodha_broker._symbol_index("NFO", rows) is first
    assert zerodha_broker._symbol_index("NFO", list(rows)) is not first  # a new download: built again
