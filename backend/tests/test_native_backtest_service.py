import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.backtest import BacktestStatus, NativeBacktestJob, NativeBacktestResult, NativeBacktestTrade
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.strategy import Strategy, StrategyVersion
from app.services.backtest import native_runner as native_backtest_runner
from app.services.backtest.native_runner import run_native_backtest_job
from app.services.broker.zerodha_broker import IST
from app.services.strategy.state_machine import StrategyStatus


@pytest.fixture(autouse=True)
def _use_test_db_for_background_task(db_engine, monkeypatch):
    """run_native_backtest_job opens its own session via a direct
    `AsyncSessionLocal` import (it's a background-task entry point, same
    as run_backtest_job) -- a different binding than the db_session fixture,
    so without this it would silently operate against the real dev
    database instead of this test's isolated one (same reasoning as
    conftest.py's `client` fixture, which can't help here since these
    tests call the service function directly, not through the API)."""
    monkeypatch.setattr(native_backtest_runner, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))

# Any weekday outside 2026 (the only year seeded in nse_holidays.STATIC_HOLIDAYS)
# is a guaranteed trading day for this test, regardless of hand-verified weekday math.
_TEST_DATE = date(2025, 6, 2)
if _TEST_DATE.weekday() >= 5:
    _TEST_DATE += timedelta(days=7 - _TEST_DATE.weekday())

_ENTRY_CODE = (
    "from datetime import datetime\n"
    "from sqlalchemy import select\n"
    "from app.models.instrument import Instrument\n"
    "async def evaluate(ctx):\n"
    "    inst = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == 'BTNATIVE'))).scalar_one()\n"
    "    if ctx.state.get('force_exit'):\n"
    "        pos = ctx.state.get('position')\n"
    "        if pos:\n"
    "            exit_price = await ctx.get_price(inst.id)\n"
    "            await ctx.close_leg(inst, 'buy', 10.0, exit_price)\n"
    "            pnl = (pos['entry_price'] - exit_price) * 10.0\n"
    "            await ctx.record_trade(\n"
    "                legs=[{'instrument_id': str(inst.id), 'side': 'short', 'quantity': 10.0, 'entry_price': pos['entry_price'], 'exit_price': exit_price}],\n"
    "                pnl=pnl, pnl_pct=(pnl / (pos['entry_price'] * 10.0)) * 100, exit_reason='force_exit',\n"
    "                opened_at=datetime.fromisoformat(pos['opened_at']),\n"
    "            )\n"
    "            ctx.state['position'] = None\n"
    "        ctx.note('exited')\n"
    "        return\n"
    "    if not ctx.state.get('position'):\n"
    "        price = await ctx.get_price(inst.id)\n"
    "        if price is None:\n"
    "            ctx.note('hold', reason='no price yet')\n"
    "            return\n"
    "        await ctx.open_leg(inst, 'sell', 10.0, price)\n"
    "        ctx.state['position'] = {'instrument_id': str(inst.id), 'entry_price': price, 'opened_at': ctx.now.isoformat()}\n"
    "        ctx.note('entered', signal='SHORT')\n"
    "        return\n"
    "    ctx.note('hold')\n"
)


async def _setup(db_session: AsyncSession, python_code: str) -> dict:
    from app.models.user import Role, User, UserRole

    role = Role(name=f"native_bt_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"native_bt_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Native Backtest User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    strategy = Strategy(name="Native Backtest Strategy", owner_id=user.id, code_type="native")
    db_session.add(strategy)
    await db_session.flush()

    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[], parameters={},
        entry_rules=None, exit_rules=None, python_code=python_code,
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()

    instrument = Instrument(
        exchange="NSE", symbol="BTNATIVE", name="Backtest Native Co", instrument_type="equity",
        data_source="zerodha_kite", external_ref="BTNATIVE",
    )
    db_session.add(instrument)
    await db_session.flush()

    # Entry candle sits just before market open so the very first 09:15 IST
    # tick already sees a price; the exit candle sits at the last tick
    # (15:25, just before the 15:30 close the replay force-exits at).
    entry_ts = datetime.combine(_TEST_DATE, datetime.min.time(), tzinfo=IST).replace(hour=9, minute=10).astimezone(timezone.utc)
    exit_ts = datetime.combine(_TEST_DATE, datetime.min.time(), tzinfo=IST).replace(hour=15, minute=25).astimezone(timezone.utc)
    db_session.add_all([
        OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=entry_ts, open=100.0, high=100.0, low=100.0, close=100.0, source="zerodha_kite"),
        OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=exit_ts, open=120.0, high=120.0, low=120.0, close=120.0, source="zerodha_kite"),
    ])
    await db_session.flush()

    job = NativeBacktestJob(
        strategy_id=strategy.id, strategy_version_id=version.id, start_date=_TEST_DATE, end_date=_TEST_DATE,
        initial_capital=100000.0, requested_by=user.id,
    )
    db_session.add(job)
    await db_session.commit()

    return {"job": job, "strategy": strategy, "instrument": instrument}


async def test_run_native_backtest_job_replays_and_saves_every_trade(db_session: AsyncSession):
    ctx = await _setup(db_session, _ENTRY_CODE)
    await run_native_backtest_job(ctx["job"].id)

    await db_session.refresh(ctx["job"])
    assert ctx["job"].status == BacktestStatus.COMPLETED.value
    assert ctx["job"].error_message is None

    trades = (
        await db_session.execute(select(NativeBacktestTrade).where(NativeBacktestTrade.job_id == ctx["job"].id))
    ).scalars().all()
    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "force_exit"
    assert trade.pnl == -200.0  # sold at 100, forced to buy back at 120, 10 qty
    assert trade.legs[0]["side"] == "short"
    assert trade.legs[0]["entry_price"] == 100.0
    assert trade.legs[0]["exit_price"] == 120.0

    result = (
        await db_session.execute(select(NativeBacktestResult).where(NativeBacktestResult.job_id == ctx["job"].id))
    ).scalar_one()
    assert result.metrics["trade_count"] == 1
    assert result.metrics["net_pnl"] == -200.0
    assert result.metrics["win_rate_pct"] == 0.0
    assert result.metrics["final_capital"] == 100000.0 - 200.0
    assert len(result.equity_curve) == 2  # starting point + one trade close

    await db_session.refresh(ctx["strategy"])
    assert ctx["strategy"].status == StrategyStatus.BACKTESTED.value


async def test_run_native_backtest_job_marks_job_failed_on_broken_code(db_session: AsyncSession):
    ctx = await _setup(db_session, "async def evaluate(ctx):\n    raise ValueError('boom')\n")
    await run_native_backtest_job(ctx["job"].id)

    await db_session.refresh(ctx["job"])
    assert ctx["job"].status == BacktestStatus.FAILED.value
    assert "boom" in ctx["job"].error_message

    # A failed replay must not fabricate a backtested strategy.
    await db_session.refresh(ctx["strategy"])
    assert ctx["strategy"].status == StrategyStatus.DRAFT.value
