"""Backfill jobs and the catalog sync must save what they fetched, and
always end in a clear state -- each case below was reproduced against
Postgres 16 before the fix:

- a long source error message overflowed error_message (1000 chars) and
  the job stayed "running" forever;
- any unexpected exception did the same, and in "Backfill All" (one
  background task running every job in turn) also stopped every job
  queued behind it, which stayed "pending";
- two overlapping backfills of the same symbol failed each other on the
  unique (symbol, timeframe, ts) constraint;
- a range longer than Kite allows per request failed outright, saving
  nothing;
- the catalog sync shared one transaction across up to 100 symbols, so one
  failure rolled back all of them, every tick.
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.backfill_platform import BfBackfillJob, BfBackfillStatus, BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform import catalog_sync, catalog_sync_scheduler, jobs
from app.services.broker.zerodha_broker import KiteAPIError

IST = timezone(timedelta(hours=5, minutes=30))
KITE_MAX_DAYS = {"1m": 60, "15m": 200, "1d": 2000}
END = date(2026, 9, 24)


class FakeKite:
    """Behaves like Kite's historical endpoint: rejects a request longer
    than the interval allows, returns IST-stamped candles for weekdays."""

    def __init__(self, fail_on_call: int | None = None, error: Exception | None = None):
        self.calls: list[tuple[datetime, datetime]] = []
        self.fail_on_call = fail_on_call
        self.error = error

    async def get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        self.calls.append((start, end))
        if self.error is not None and (self.fail_on_call is None or len(self.calls) == self.fail_on_call):
            raise self.error
        if (end - start).days > KITE_MAX_DAYS[timeframe]:
            raise KiteAPIError(f"interval exceeds max limit: {KITE_MAX_DAYS[timeframe]} days")
        step = {"1m": 1, "15m": 15}[timeframe]
        bars, d = [], start.astimezone(IST).date()
        while d <= end.astimezone(IST).date():
            if d.weekday() < 5:
                t = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
                while t < datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST):
                    bars.append({"ts": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10})
                    t += timedelta(minutes=step)
            d += timedelta(days=1)
        return bars


@pytest.fixture
def sessions(db_engine, monkeypatch):
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(jobs, "AsyncSessionLocal", factory)
    monkeypatch.setattr(catalog_sync_scheduler, "AsyncSessionLocal", factory)
    monkeypatch.setattr(jobs, "_KITE_PACING_SECONDS", 0, raising=False)
    return factory


def use_kite(monkeypatch, kite: FakeKite) -> FakeKite:
    async def broker(db, user_id):
        return kite
    monkeypatch.setattr(jobs, "get_authenticated_kite_broker", broker)
    return kite


async def _symbol(db: AsyncSession, name: str) -> BfSymbol:
    symbol = BfSymbol(source="zerodha", symbol=name, display_name=name)
    db.add(symbol)
    await db.commit()
    return symbol


async def _job(db: AsyncSession, symbol: BfSymbol, timeframe: str, days: int, end: date = END) -> uuid.UUID:
    job = BfBackfillJob(symbol_id=symbol.id, source="zerodha", timeframe=timeframe, start_date=end - timedelta(days=days), end_date=end)
    db.add(job)
    await db.commit()
    return job.id


async def _job_row(sessions, job_id) -> BfBackfillJob:
    async with sessions() as db:
        return await db.get(BfBackfillJob, job_id)


async def _bar_count(sessions, symbol_id) -> int:
    async with sessions() as db:
        return (await db.execute(select(func.count()).select_from(BfOhlcvBar).where(BfOhlcvBar.symbol_id == symbol_id))).scalar_one()


async def test_a_long_source_error_fails_the_job_with_its_reason(db_session, sessions, monkeypatch):
    use_kite(monkeypatch, FakeKite(error=KiteAPIError("Kite API error 500: " + "<html>upstream error</html> " * 120)))
    job_id = await _job(db_session, await _symbol(db_session, "LONGERR"), "15m", 10)

    await jobs.run_bf_backfill_job(job_id)

    job = await _job_row(sessions, job_id)
    assert job.status == BfBackfillStatus.FAILED.value
    assert job.error_message.startswith("Kite API error 500")
    assert len(job.error_message) <= 1000


async def test_an_unexpected_error_fails_the_job_instead_of_raising(db_session, sessions, monkeypatch):
    use_kite(monkeypatch, FakeKite(error=httpx.ReadError("connection reset by peer")))
    job_id = await _job(db_session, await _symbol(db_session, "READERR"), "15m", 10)

    await jobs.run_bf_backfill_job(job_id)  # must not raise

    job = await _job_row(sessions, job_id)
    assert job.status == BfBackfillStatus.FAILED.value
    assert job.error_message == "ReadError: connection reset by peer"


async def test_backfill_all_carries_on_after_a_failing_job(db_session, sessions, monkeypatch):
    job_ids = [await _job(db_session, await _symbol(db_session, f"QUEUE{i}"), "15m", 10) for i in range(3)]

    # What Starlette's BackgroundTasks does with a "Backfill All": each job
    # in turn, in one task -- the second one hits a network error.
    for n, job_id in enumerate(job_ids):
        use_kite(monkeypatch, FakeKite(error=httpx.ReadError("reset")) if n == 1 else FakeKite())
        await jobs.run_bf_backfill_job(job_id)

    statuses = [(await _job_row(sessions, j)).status for j in job_ids]
    assert statuses == ["completed", "failed", "completed"]


async def test_a_long_range_is_fetched_in_parts_kite_accepts(db_session, sessions, monkeypatch):
    kite = use_kite(monkeypatch, FakeKite())
    symbol = await _symbol(db_session, "LONGRANGE")
    job_id = await _job(db_session, symbol, "1m", 120)

    await jobs.run_bf_backfill_job(job_id)

    job = await _job_row(sessions, job_id)
    assert job.status == BfBackfillStatus.COMPLETED.value, job.error_message
    assert len(kite.calls) == 3
    assert all((end - start).days <= 60 for start, end in kite.calls)
    assert job.duplicate_count == 0  # the bar on a boundary between two parts counts once
    assert job.inserted_count == job.downloaded_count == await _bar_count(sessions, symbol.id)


async def test_a_failing_part_keeps_the_bars_already_fetched(db_session, sessions, monkeypatch):
    use_kite(monkeypatch, FakeKite(fail_on_call=2, error=KiteAPIError("Too many requests")))
    symbol = await _symbol(db_session, "PARTIAL")
    job_id = await _job(db_session, symbol, "1m", 120)

    await jobs.run_bf_backfill_job(job_id)

    job = await _job_row(sessions, job_id)
    assert job.status == BfBackfillStatus.FAILED.value
    assert job.inserted_count > 0 and job.inserted_count == await _bar_count(sessions, symbol.id)
    assert job.error_message.startswith(f"Saved {job.inserted_count} new bars, then: Part 2 of 3")


async def test_overlapping_backfills_of_one_symbol_both_complete(db_session, sessions, monkeypatch):
    use_kite(monkeypatch, FakeKite())
    symbol = await _symbol(db_session, "OVERLAP")
    first = await _job(db_session, symbol, "15m", 30)
    second = await _job(db_session, symbol, "15m", 20)

    await asyncio.gather(jobs.run_bf_backfill_job(first), jobs.run_bf_backfill_job(second))

    a, b = await _job_row(sessions, first), await _job_row(sessions, second)
    assert (a.status, b.status) == ("completed", "completed")
    total = await _bar_count(sessions, symbol.id)
    assert a.inserted_count + b.inserted_count == total == a.downloaded_count


async def _completed_symbol_with_bars(db: AsyncSession, name: str, bars: int, timeframes=("15m",)) -> BfSymbol:
    symbol = await _symbol(db, name)
    db.add(BfBackfillJob(
        symbol_id=symbol.id, source="zerodha", timeframe=timeframes[0], status=BfBackfillStatus.COMPLETED.value,
        completed_at=datetime.now(timezone.utc),
    ))
    for tf in timeframes:
        for i in range(bars):
            db.add(BfOhlcvBar(
                symbol_id=symbol.id, timeframe=tf, ts=datetime(2026, 9, 1, 3, 45, tzinfo=timezone.utc) + timedelta(minutes=15 * i),
                open=1, high=1, low=1, close=1, volume=1,
            ))
    await db.commit()
    return symbol


async def test_one_symbol_failing_to_sync_does_not_hold_back_the_others(db_session, sessions, monkeypatch):
    good_a = await _completed_symbol_with_bars(db_session, "GOODA", 3)
    bad = await _completed_symbol_with_bars(db_session, "BADONE", 3)
    good_b = await _completed_symbol_with_bars(db_session, "GOODB", 3)
    real_sync = catalog_sync_scheduler.sync_symbol_to_catalog

    async def flaky_sync(db, symbol):
        if symbol.symbol == "BADONE":
            await real_sync(db, symbol)  # writes, then fails before commit
            raise RuntimeError("simulated save failure")
        return await real_sync(db, symbol)

    monkeypatch.setattr(catalog_sync_scheduler, "sync_symbol_to_catalog", flaky_sync)
    scheduler = catalog_sync_scheduler.CatalogSyncScheduler()

    synced_symbols, synced_bars = await scheduler.sync_pending()

    assert (synced_symbols, synced_bars) == (2, 6)
    assert scheduler.last_failures == ["BADONE: RuntimeError: simulated save failure"]
    async with sessions() as db:
        synced = {s.symbol: s.last_synced_at for s in (await db.execute(select(BfSymbol))).scalars()}
        instruments = {i.symbol for i in (await db.execute(select(Instrument))).scalars()}
    assert synced["GOODA"] and synced["GOODB"] and synced["BADONE"] is None  # retried next tick
    assert {"GOODA", "GOODB"} <= instruments and "BADONE" not in instruments  # its partial write rolled back


async def test_catalog_sync_copies_in_pages_and_skips_what_is_there(db_session, sessions, monkeypatch):
    monkeypatch.setattr(catalog_sync, "_PAGE_SIZE", 3, raising=False)
    symbol = await _completed_symbol_with_bars(db_session, "PAGED", 5, timeframes=("15m", "1d"))

    first = await catalog_sync.sync_symbol_to_catalog(db_session, symbol)
    await db_session.commit()
    second = await catalog_sync.sync_symbol_to_catalog(db_session, symbol)
    await db_session.commit()

    assert (first.bars_synced, first.bars_skipped) == (10, 0)
    assert (second.bars_synced, second.bars_skipped) == (0, 10)
    count = (await db_session.execute(select(func.count()).select_from(OhlcvCandle))).scalar_one()
    assert count == 10
