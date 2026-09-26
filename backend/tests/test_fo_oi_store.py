"""services/fo_scan: the OI store (captures, totals, the two-session prune,
yesterday's baseline), its scheduler windows, and the native runner's
exact-time wake-ups."""

import uuid
from datetime import date, datetime, time as dtime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.fo_scan import MARK_0920, MARK_CLOSE, MARK_PRE_OPEN, FoOiSnapshot, FoOiTotal
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.broker.zerodha_broker import IST
from app.services.fo_scan import oi_store, oi_store_scheduler, pacing
from app.services.paper_trading import scheduler as paper_scheduler

EXPIRY = date(2026, 10, 27)
NEXT = date(2026, 11, 23)


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(pacing.quote_pacer, "interval", 0)
    monkeypatch.setattr(pacing.history_pacer, "interval", 0)


class QuoteKite:
    def __init__(self, oi: float = 1000.0):
        self.oi = oi
        self.batches: list[int] = []

    async def get_quote_batch(self, keys):
        self.batches.append(len(keys))
        return {k: {"oi": self.oi, "volume": 5, "last_price": 2.0} for k in keys}


async def _stock(db: AsyncSession, symbol: str, strikes=(100.0, 102.0)) -> Instrument:
    eq = Instrument(exchange="NSE", symbol=symbol, name=symbol, instrument_type="equity", data_source="zerodha_kite", external_ref=symbol)
    db.add(eq)
    await db.flush()
    rows = []
    for expiry, tag in ((EXPIRY, "OCT"), (NEXT, "NOV")):
        rows.append(Instrument(exchange="NFO", symbol=f"{symbol}{tag}FUT", name="f", instrument_type="future", data_source="zerodha_kite",
                               external_ref=f"{symbol}{tag}FUT", expiry=expiry, lot_size=100, underlying_instrument_id=eq.id))
        for k in strikes:
            for t in ("CE", "PE"):
                rows.append(Instrument(exchange="NFO", symbol=f"{symbol}{tag}{int(k)}{t}", name="o", instrument_type="option",
                                       data_source="zerodha_kite", external_ref=f"{symbol}{tag}{int(k)}{t}", expiry=expiry, strike=k,
                                       option_type=t, lot_size=100, underlying_instrument_id=eq.id))
    db.add_all(rows)
    await db.flush()
    return eq


async def test_contracts_are_the_current_month_plus_next_month_on_expiry_day(db_session: AsyncSession):
    await _stock(db_session, "AAA")
    await _stock(db_session, "BBB")
    index_fut = Instrument(exchange="NFO", symbol="NIFTYOCTFUT", name="n", instrument_type="future", data_source="zerodha_kite",
                           external_ref="NIFTYOCTFUT", expiry=EXPIRY)
    db_session.add(index_fut)
    await db_session.commit()

    contracts = await oi_store.stock_contracts(db_session, date(2026, 10, 14))
    assert len(contracts) == 2 * 5  # per stock: future + 2 CE + 2 PE, October only; no index future
    assert {c.expiry for c in contracts} == {EXPIRY}
    assert sorted({c.kind for c in contracts}) == ["CE", "FUT", "PE"]

    on_expiry = await oi_store.stock_contracts(db_session, EXPIRY, next_month_on_expiry=True)
    assert len(on_expiry) == 2 * 10 and {c.expiry for c in on_expiry} == {EXPIRY, NEXT}


async def test_capture_saves_contracts_and_per_stock_totals_and_prune_keeps_two_sessions(db_session: AsyncSession):
    await _stock(db_session, "AAA")
    await db_session.commit()
    kite = QuoteKite(oi=1000.0)
    contracts = await oi_store.stock_contracts(db_session, date(2026, 10, 12))
    for day in (date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)):
        result = await oi_store.capture(db_session, kite, day, MARK_CLOSE, contracts)
        assert result == {"contracts": 5, "quoted": 5, "with_oi": 5, "error": None}
    total = (await db_session.execute(select(FoOiTotal).where(FoOiTotal.session_date == date(2026, 10, 14)))).scalar_one()
    assert (total.symbol, total.fut_oi, total.ce_oi, total.pe_oi, total.total_oi) == ("AAA", 1000.0, 2000.0, 2000.0, 5000.0)

    # Capturing the same mark again replaces it.
    await oi_store.capture(db_session, QuoteKite(oi=2000.0), date(2026, 10, 14), MARK_CLOSE, contracts)
    count = (await db_session.execute(select(func.count()).select_from(FoOiSnapshot).where(FoOiSnapshot.session_date == date(2026, 10, 14)))).scalar_one()
    assert count == 5

    assert await oi_store.prune(db_session, date(2026, 10, 14)) == 5  # the 12th goes, the 13th stays
    days = (await db_session.execute(select(FoOiSnapshot.session_date).distinct())).scalars().all()
    assert sorted(days) == [date(2026, 10, 13), date(2026, 10, 14)]
    assert (await db_session.execute(select(func.count()).select_from(FoOiTotal))).scalar_one() == 3  # totals are kept


async def test_previous_close_prefers_yesterdays_close_then_todays_pre_open(db_session: AsyncSession):
    await _stock(db_session, "AAA")
    await db_session.commit()
    contracts = await oi_store.stock_contracts(db_session, date(2026, 10, 14))
    ids = [c.instrument_id for c in contracts]
    assert await oi_store.previous_close(db_session, date(2026, 10, 14), ids) == ({}, None)

    await oi_store.capture(db_session, QuoteKite(oi=700.0), date(2026, 10, 14), MARK_PRE_OPEN, contracts)
    prev, label = await oi_store.previous_close(db_session, date(2026, 10, 14), ids)
    assert label == "pre_open" and set(prev.values()) == {700.0}

    await oi_store.capture(db_session, QuoteKite(oi=900.0), date(2026, 10, 13), MARK_CLOSE, contracts)
    prev, label = await oi_store.previous_close(db_session, date(2026, 10, 14), ids)
    assert label == "close" and set(prev.values()) == {900.0} and len(prev) == 5
    # Monday 12 Oct's baseline is Friday 9 Oct's close.
    await oi_store.capture(db_session, QuoteKite(oi=500.0), date(2026, 10, 9), MARK_CLOSE, contracts)
    prev, label = await oi_store.previous_close(db_session, date(2026, 10, 12), ids)
    assert label == "close" and set(prev.values()) == {500.0}


def test_scheduler_windows():
    s = oi_store_scheduler.FoOiStoreScheduler()

    def at(h, m, sec=0, d=date(2026, 10, 14)):
        return datetime.combine(d, dtime(h, m, sec), tzinfo=IST).astimezone(timezone.utc)

    assert s.due(at(9, 9, 59)) is None
    assert s.due(at(9, 10)) == MARK_PRE_OPEN
    assert s.due(at(9, 20, 14)) is None
    assert s.due(at(9, 20, 15)) == MARK_0920
    assert s.due(at(15, 30, 59)) is None
    assert s.due(at(15, 31)) == MARK_CLOSE
    assert s.due(at(15, 31, d=date(2026, 10, 17))) is None  # Saturday
    assert s.due(at(15, 31, d=date(2026, 10, 20))) is None  # Dussehra


async def test_scheduler_close_capture_runs_once_and_skips_the_pre_open_backup(db_engine, db_session: AsyncSession, monkeypatch):
    await _stock(db_session, "AAA")
    await db_session.commit()
    kite = QuoteKite()
    monkeypatch.setattr(oi_store_scheduler, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))

    async def _kite(db):
        return kite

    monkeypatch.setattr(oi_store_scheduler, "kite_broker", _kite)
    s = oi_store_scheduler.FoOiStoreScheduler()
    close_at = datetime.combine(date(2026, 10, 13), dtime(15, 31), tzinfo=IST).astimezone(timezone.utc)
    await s.tick(close_at)
    assert s.last_capture["mark"] == MARK_CLOSE and s.last_capture["quoted"] == 5
    await s.tick(close_at.replace(minute=close_at.minute + 5))
    assert len(kite.batches) == 1  # done: not captured again

    # Next morning: yesterday's close is there, so no pre-open backup.
    fresh = oi_store_scheduler.FoOiStoreScheduler()
    await fresh.tick(datetime.combine(date(2026, 10, 14), dtime(9, 10), tzinfo=IST).astimezone(timezone.utc))
    assert len(kite.batches) == 1 and (date(2026, 10, 14), MARK_PRE_OPEN) in fresh._done


# ---------------------------------------------------------------------
# Exact-time wake-ups
# ---------------------------------------------------------------------
WAKE_CODE = """
from datetime import timedelta

async def evaluate(ctx):
    ctx.state['runs'] = ctx.state.get('runs', 0) + 1
    ctx.wake_at(ctx.now + timedelta(seconds=7))
    ctx.note('hold', reason=f"run {ctx.state['runs']}")
"""


async def _native_deployment(db: AsyncSession, code: str) -> PaperNativeDeployment:
    role = Role(name=f"wk_{uuid.uuid4().hex[:6]}", description="x")
    db.add(role)
    await db.flush()
    user = User(email=f"wk_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="W")
    user.user_roles = [UserRole(role=role)]
    db.add(user)
    await db.flush()
    strategy = Strategy(name="Wake", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="5m", instrument_ids=[], parameters={},
                              python_code=code, position_sizing={}, risk_rules={})
    portfolio = PaperPortfolio(user_id=user.id, cash=1000.0, initial_capital=1000.0)
    db.add_all([version, portfolio])
    await db.flush()
    deployment = PaperNativeDeployment(portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                       status=DeploymentStatus.ACTIVE.value)
    db.add(deployment)
    await db.commit()
    return deployment


async def test_wake_ups_run_a_native_strategy_at_the_second_it_asked_for(db_engine, db_session: AsyncSession, monkeypatch):
    deployment = await _native_deployment(db_session, WAKE_CODE)
    monkeypatch.setattr(paper_scheduler, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))
    sched = paper_scheduler.PaperTradingScheduler()
    t0 = datetime.combine(date(2026, 10, 14), dtime(9, 20), tzinfo=IST).astimezone(timezone.utc)

    assert await sched.tick_once(db_session, now=t0) == 1  # the regular cycle
    assert deployment.id in sched._wakeups  # it asked to run again

    wake = t0.replace(second=7)
    sched._wakeups[deployment.id] = wake
    assert await sched.run_due_wakeups(t0.replace(second=3)) == 0  # not yet
    assert await sched.run_due_wakeups(wake) == 1
    await db_session.refresh(deployment)
    assert deployment.state["runs"] == 2  # the wake-up saw the regular cycle's state, not a stale copy
    assert deployment.id in sched._wakeups

    # Outside market hours nothing runs.
    sched._wakeups[deployment.id] = t0.replace(hour=12)  # 17:30 IST
    assert await sched.run_due_wakeups(t0.replace(hour=13)) == 0


async def test_built_in_native_strategy_code_can_be_listed_and_loaded(client, seeded_admin):
    resp = await client.post("/api/v1/auth/login", json={"email": seeded_admin["email"], "password": seeded_admin["password"]})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    listed = (await client.get("/api/v1/strategies/native-builtins", headers=headers)).json()
    fo = next(b for b in listed if b["name"] == "fo_opening_momentum")
    assert fo["version"] == 6 and "FLY OI SCN" in fo["title"] and "code" not in fo
    assert all(not b["name"].startswith("_") for b in listed)

    loaded = (await client.get("/api/v1/strategies/native-builtins/fo_opening_momentum", headers=headers)).json()
    assert "async def evaluate(ctx)" in loaded["code"]
    assert (await client.get("/api/v1/strategies/native-builtins/__init__", headers=headers)).status_code == 404
    assert (await client.get("/api/v1/strategies/native-builtins/nope", headers=headers)).status_code == 404
