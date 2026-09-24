"""MACD - RSI - 15 MIN (native_strategies/macd_rsi_15min.py). The case that
matters: SOLARINDS's MACD line crossed below zero while held, but by the
time a tick looked, the cross was no longer the newest candle -- the old
exit rule (only the newest candle counts) kept the stock indefinitely and
so never freed the slot for a replacement."""

import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.market_data import active_timeframe_sync_scheduler as sync_module
from app.services.paper_trading.native_runner import NativeContext
from app.services.strategy.native_strategies.macd_rsi_15min import BAR_LENGTH, MIN_BARS, compute_signal, evaluate

START = datetime(2026, 9, 1, 3, 45, tzinfo=timezone.utc)  # 09:15 IST


def _bars(closes: list[float]) -> list[dict]:
    return [{"ts": START + i * BAR_LENGTH, "close": c} for i, c in enumerate(closes)]


def _wave(n: int) -> list[float]:
    return [500 + 20 * math.sin(2 * math.pi * i / 100) for i in range(n)]


def _down_cross_indices(closes: list[float]) -> list[int]:
    """Bar indices where the MACD line crosses below zero -- EMAs are
    causal, so evaluating each prefix gives the same crosses the full
    series has."""
    return [i for i in range(MIN_BARS - 1, len(closes)) if compute_signal(_bars(closes[: i + 1]))["sell"]]


def _solarinds_closes() -> tuple[list[float], int]:
    """A wave cut three candles after its last down-cross, and that cross's index."""
    wave = _wave(300)
    cross = _down_cross_indices(wave)[-1]
    return wave[: cross + 4], cross


def _sbin_closes() -> list[float]:
    """Falls, then rises: one up-cross, still buy-eligible at the end."""
    return [400 - 0.5 * i for i in range(150)] + [325 + 1.0 * i for i in range(100)]


def test_last_sell_at_is_the_close_of_the_latest_down_cross_candle():
    closes, cross = _solarinds_closes()
    sig = compute_signal(_bars(closes))
    # The old rule only looked at the newest candle -- three candles past the cross, it says "no sell".
    assert sig["sell"] is False
    assert sig["active"] is False
    assert sig["last_sell_at"] == START + (cross + 1) * BAR_LENGTH


def test_trades_the_macd_line_zero_cross_not_the_signal_line():
    """Entries and exits follow the MACD line (EMA12 - EMA26) crossing zero.
    The signal line (EMA9 of MACD) lags it, so its cross comes later."""
    import pandas as pd

    closes = _wave(300)
    c = pd.Series(closes)
    macd = c.ewm(span=12, adjust=False, min_periods=12).mean() - c.ewm(span=26, adjust=False, min_periods=26).mean()
    signal_line = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    macd_down = [i for i in range(MIN_BARS - 1, 300) if macd[i - 1] > 0 > macd[i]]
    signal_down = [i for i in range(MIN_BARS - 1, 300) if signal_line[i - 1] > 0 > signal_line[i]]

    assert _down_cross_indices(closes) == macd_down
    assert signal_down[0] > macd_down[0]  # the signal line would have sold later


def test_not_enough_history():
    assert compute_signal(_bars(_wave(MIN_BARS - 1))) is None


async def _setup(db_session: AsyncSession, holding_opened_at: datetime):
    role = Role(name=f"macd_rsi_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"macd_rsi_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="MACD RSI User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()
    strategy = Strategy(name="MACD - RSI - 15 MIN", owner_id=user.id, code_type="native")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
        entry_rules=None, exit_rules=None, python_code="# see native_strategies/macd_rsi_15min.py",
        position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={},
    )
    db_session.add(version)
    await db_session.flush()
    portfolio = PaperPortfolio(user_id=user.id, cash=1_000_000.0, initial_capital=1_000_000.0)
    db_session.add(portfolio)
    await db_session.flush()
    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
        status=DeploymentStatus.ACTIVE.value, state=None,
    )
    db_session.add(deployment)

    instruments = {}
    for symbol, closes in (("SOLARINDS", _solarinds_closes()[0]), ("SBIN", _sbin_closes())):
        instrument = Instrument(exchange="NSE", symbol=symbol, name=symbol, instrument_type="equity", data_source="zerodha_kite", external_ref=symbol)
        db_session.add(instrument)
        await db_session.flush()
        for bar in _bars(closes):
            db_session.add(OhlcvCandle(
                instrument_id=instrument.id, timeframe="15m", ts=bar["ts"],
                open=bar["close"], high=bar["close"], low=bar["close"], close=bar["close"], volume=1, source="test",
            ))
        instruments[symbol] = instrument
    await db_session.commit()

    solarinds = instruments["SOLARINDS"]
    # Five of five slots taken, as in the live deployment -- SBIN can only
    # come in if SOLARINDS goes out. The other four have no candles here,
    # so they're simply held.
    holdings = {
        f"HELD{i}": {"instrument_id": str(uuid.uuid4()), "quantity": 10.0, "entry_price": 100.0, "opened_at": START.isoformat()}
        for i in range(4)
    }
    holdings["SOLARINDS"] = {
        "instrument_id": str(solarinds.id), "quantity": 100.0, "entry_price": 510.0, "opened_at": holding_opened_at.isoformat(),
    }
    state = {"seeded": True, "holdings": holdings}
    solarinds_closes = _solarinds_closes()[0]
    now = START + len(solarinds_closes) * BAR_LENGTH + timedelta(minutes=1)
    ctx = NativeContext(db=db_session, portfolio=portfolio, deployment=deployment, state=state, now=now)
    return ctx, deployment, instruments


async def test_missed_down_cross_still_exits_and_frees_the_slot(db_session: AsyncSession):
    _, cross = _solarinds_closes()
    opened_before_cross = START + (cross - 20) * BAR_LENGTH
    ctx, deployment, _ = await _setup(db_session, opened_before_cross)

    await evaluate(ctx)

    assert "SOLARINDS" not in ctx.state["holdings"]
    assert "SBIN" in ctx.state["holdings"]  # the freed slot is refilled
    assert "sold: SOLARINDS" in ctx._last_reason
    assert "bought: SBIN" in ctx._last_reason
    await db_session.commit()
    trade = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == deployment.id))).scalar_one()
    assert trade.exit_reason == "macd_zero_cross_down"


async def test_holding_bought_after_the_down_cross_is_kept(db_session: AsyncSession):
    """A seeded holding bought while the MACD was already below zero waits
    for the next down-cross -- the one before it was bought doesn't count."""
    _, cross = _solarinds_closes()
    opened_after_cross = START + (cross + 2) * BAR_LENGTH
    ctx, _, _ = await _setup(db_session, opened_after_cross)

    await evaluate(ctx)

    assert "SOLARINDS" in ctx.state["holdings"]
    assert "sold" not in ctx._last_reason
    assert "SBIN" not in ctx.state["holdings"]  # still 5/5, no slot to fill


async def test_holding_is_kept_when_the_macd_already_crossed_back_up(db_session: AsyncSession, monkeypatch):
    """Down-cross after entry, then back above zero before a tick saw it:
    hold on, rather than sell and buy the same stock straight back."""
    import tests.test_macd_rsi_15min as this

    wave = _wave(300)
    down = _down_cross_indices(wave)[0]
    up = next(i for i in range(down + 1, len(wave)) if compute_signal(_bars(wave[: i + 1]))["active"])
    closes = wave[: up + 3]
    monkeypatch.setattr(this, "_solarinds_closes", lambda: (closes, down))
    ctx, _, _ = await _setup(db_session, START + (down - 20) * BAR_LENGTH)
    assert compute_signal(_bars(closes))["last_sell_at"] > START + (down - 20) * BAR_LENGTH

    await evaluate(ctx)

    assert "SOLARINDS" in ctx.state["holdings"]
    assert "sold" not in ctx._last_reason


async def test_get_candles_leaves_out_the_forming_candle_and_keeps_the_pair_synced(db_session: AsyncSession):
    ctx, _, instruments = await _setup(db_session, START)
    sbin = instruments["SBIN"]
    closes = _sbin_closes()
    forming_ts = START + len(closes) * BAR_LENGTH
    db_session.add(OhlcvCandle(instrument_id=sbin.id, timeframe="15m", ts=forming_ts, open=1, high=1, low=1, close=1, volume=1, source="test"))
    await db_session.commit()
    ctx.now = forming_ts + timedelta(minutes=5)  # mid-candle

    bars = await ctx.get_candles(sbin.id, "15m", 300)

    assert len(bars) == len(closes)
    assert bars[-1]["ts"] == forming_ts - BAR_LENGTH
    assert bars[0]["ts"] < bars[-1]["ts"]  # oldest first
    assert (sbin.id, "15m") in sync_module._native_pairs(ctx.now)


async def test_repeated_ticks_sell_once_and_buy_the_replacement_once(db_engine, db_session: AsyncSession):
    """Through the live runner, reloading state from the database every
    tick as the scheduler does: SOLARINDS is sold once and SBIN bought once,
    however many ticks follow. Before the runner deep-copied state, each
    tick reloaded the unsaved old holdings and sold the same stocks again."""
    from pathlib import Path

    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.services.strategy.native_strategies.macd_rsi_15min as strategy_module
    from app.services.paper_trading.native_runner import run_native_strategy

    _, cross = _solarinds_closes()
    ctx, deployment, _ = await _setup(db_session, START + (cross - 20) * BAR_LENGTH)
    version = await db_session.get(StrategyVersion, deployment.strategy_version_id)
    version.python_code = Path(strategy_module.__file__).read_text()
    deployment.state = ctx.state
    await db_session.commit()
    cash_before = (await db_session.get(PaperPortfolio, deployment.portfolio_id)).cash

    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    for _ in range(4):
        async with sessions() as db:
            outcome = await run_native_strategy(db, await db.get(PaperNativeDeployment, deployment.id))
            assert outcome.action != "error", outcome.reason

    async with sessions() as db:
        saved = await db.get(PaperNativeDeployment, deployment.id)
        trades = (await db.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == deployment.id))).scalars().all()
        cash_after = (await db.get(PaperPortfolio, deployment.portfolio_id)).cash
    assert "SOLARINDS" not in saved.state["holdings"]
    assert "SBIN" in saved.state["holdings"]
    assert len(trades) == 1
    sbin = saved.state["holdings"]["SBIN"]
    solarinds_exit = trades[0].legs[0]["exit_price"]
    assert cash_after == cash_before + 100 * solarinds_exit - sbin["quantity"] * sbin["entry_price"]


async def test_a_cross_on_the_forming_candle_trades_only_once_that_candle_completes(db_session: AsyncSession, monkeypatch):
    """The crossover candle is stored while still forming: nothing happens
    until it closes, then the exit goes through on the next check -- at
    the start of the following candle."""
    import tests.test_macd_rsi_15min as this

    wave = _wave(300)
    cross = _down_cross_indices(wave)[-1]
    closes = wave[: cross + 1]  # the down-cross candle is the newest stored one
    monkeypatch.setattr(this, "_solarinds_closes", lambda: (closes, cross))
    ctx, deployment, _ = await _setup(db_session, START + (cross - 20) * BAR_LENGTH)
    cross_opens = START + cross * BAR_LENGTH

    ctx.now = cross_opens + timedelta(minutes=10)  # 5 minutes before the crossover candle closes
    await evaluate(ctx)
    assert "SOLARINDS" in ctx.state["holdings"]
    assert "sold" not in ctx._last_reason

    next_check = NativeContext(
        db=db_session, portfolio=ctx.portfolio, deployment=deployment, state=ctx.state,
        now=cross_opens + BAR_LENGTH + timedelta(seconds=10),  # 10s into the next candle
    )
    await evaluate(next_check)
    assert "SOLARINDS" not in next_check.state["holdings"]
    assert "sold: SOLARINDS" in next_check._last_reason
