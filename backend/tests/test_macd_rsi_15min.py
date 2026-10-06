"""MACD - RSI - 15 MIN (native_strategies/macd_rsi_15min.py). The exit case
that matters: SOLARINDS's MACD line crossed below zero while held, but by the
time a tick looked, the cross was no longer the newest candle -- the old
exit rule (only the newest candle counts) kept the stock indefinitely and
so never freed the slot for a replacement. Entries are the opposite: only
the newest candle's up-cross counts, and only until the next one closes."""

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


def _sbin_closes(length: int) -> list[float]:
    """Falls, then rises, `length` candles ending on the MACD's up-cross. A
    flat start of the first close leaves both EMAs (seeded at it) and so the
    MACD after it unchanged -- it only moves the cross to the last candle."""
    base = [400 - 0.5 * i for i in range(150)] + [325 + 1.0 * i for i in range(100)]
    macd = _macd(base)
    up = next(i for i in range(MIN_BARS - 1, len(base)) if macd[i - 1] < 0 < macd[i])
    closes = [base[0]] * (length - up - 1) + base[: up + 1]
    assert len(closes) == length and _macd(closes)[-2] < 0 < _macd(closes)[-1]
    return closes


def _macd(closes: list[float]) -> list[float]:
    import pandas as pd

    c = pd.Series(closes)
    return list(c.ewm(span=12, adjust=False, min_periods=12).mean() - c.ewm(span=26, adjust=False, min_periods=26).mean())


def _stale(closes: list[float], candles: int) -> list[float]:
    """The same stock `candles` candles after its up-cross, still rising."""
    return closes[candles:] + [closes[-1] + 1.0 * (i + 1) for i in range(candles)]


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


def test_buy_is_the_up_cross_on_the_newest_candle_only():
    closes = _sbin_closes(250)
    sig = compute_signal(_bars(closes))
    assert sig["buy"] and sig["active"]
    assert sig["closed_at"] == START + len(closes) * BAR_LENGTH

    later = compute_signal(_bars(_stale(closes, 2)))
    assert later["active"] and not later["buy"]  # still above zero, but the cross is two candles back


async def _setup(db_session: AsyncSession, holding_opened_at: datetime, sbin_closes=None, extra: dict | None = None):
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
    solarinds_closes = _solarinds_closes()[0]
    series = {"SOLARINDS": solarinds_closes, "SBIN": sbin_closes or _sbin_closes(len(solarinds_closes)), **(extra or {})}
    for symbol, closes in series.items():
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
    closes = _sbin_closes(len(_solarinds_closes()[0]))
    forming_ts = START + len(closes) * BAR_LENGTH
    db_session.add(OhlcvCandle(instrument_id=sbin.id, timeframe="15m", ts=forming_ts, open=1, high=1, low=1, close=1, volume=1, source="test"))
    await db_session.commit()
    ctx.now = forming_ts + timedelta(minutes=5)  # mid-candle

    bars = await ctx.get_candles(sbin.id, "15m", 300)

    assert len(bars) == len(closes)
    assert bars[-1]["ts"] == forming_ts - BAR_LENGTH
    assert bars[0]["ts"] < bars[-1]["ts"]  # oldest first
    assert (sbin.id, "15m") in sync_module._native_pairs(ctx.now)


async def test_repeated_ticks_sell_once_and_buy_the_replacement_once(db_engine, db_session: AsyncSession, monkeypatch):
    """Through the live runner, reloading state from the database every
    tick as the scheduler does: SOLARINDS is sold once and SBIN bought once,
    however many ticks follow. Before the runner deep-copied state, each
    tick reloaded the unsaved old holdings and sold the same stocks again."""
    from pathlib import Path

    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.services.strategy.native_strategies.macd_rsi_15min as strategy_module
    from app.services.paper_trading.native_runner import run_native_strategy

    import tests.test_macd_rsi_15min as this

    # run_native_strategy runs at the real clock: the candles end a minute ago.
    closes, cross = _solarinds_closes()
    monkeypatch.setattr(this, "START", datetime.now(timezone.utc) - timedelta(minutes=1) - len(closes) * BAR_LENGTH)
    ctx, deployment, _ = await this._setup(db_session, this.START + (cross - 20) * BAR_LENGTH)
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


async def _sell_solarinds_setup(db_session: AsyncSession, **kwargs):
    """SOLARINDS goes out on this tick, so one slot is free for an entry."""
    _, cross = _solarinds_closes()
    return await _setup(db_session, START + (cross - 20) * BAR_LENGTH, **kwargs)


async def test_an_up_cross_from_earlier_candles_is_not_bought(db_session: AsyncSession):
    """SBIN's MACD is above zero but crossed two candles ago: the freed slot stays empty."""
    length = len(_solarinds_closes()[0])
    ctx, _, _ = await _sell_solarinds_setup(db_session, sbin_closes=_stale(_sbin_closes(length), 2))

    await evaluate(ctx)

    assert "SOLARINDS" not in ctx.state["holdings"]
    assert "SBIN" not in ctx.state["holdings"]
    assert "bought" not in ctx._last_reason
    assert len(ctx.state["holdings"]) == 4


async def test_the_up_cross_is_not_bought_once_the_next_candle_has_closed(db_session: AsyncSession):
    """The cross candle is still the newest stored one, but the candle after it
    has already closed (just not saved yet): too late to buy."""
    ctx, _, _ = await _sell_solarinds_setup(db_session)
    closed_at = compute_signal(await ctx.get_candles((await _instrument(db_session, "SBIN")).id, "15m", 300))["closed_at"]
    ctx.now = closed_at + BAR_LENGTH + timedelta(seconds=10)

    await evaluate(ctx)

    assert "SBIN" not in ctx.state["holdings"]
    assert "bought" not in ctx._last_reason


async def test_up_crosses_on_the_same_candle_go_to_the_highest_rsi(db_session: AsyncSession):
    """SBIN and BHEL cross up on the same candle; BHEL's bigger last gain gives
    it the higher RSI and the one free slot. SBIN is listed as passed over."""
    length = len(_solarinds_closes()[0])
    sbin = _sbin_closes(length)
    bhel = sbin[:-1] + [sbin[-1] + 5.0]
    assert _macd(bhel)[-2] < 0 < _macd(bhel)[-1]
    assert compute_signal(_bars(bhel))["rsi"] > compute_signal(_bars(sbin))["rsi"]
    ctx, _, _ = await _sell_solarinds_setup(db_session, sbin_closes=sbin, extra={"BHEL": bhel})

    await evaluate(ctx)

    assert "BHEL" in ctx.state["holdings"]
    assert "SBIN" not in ctx.state["holdings"]
    assert "bought: BHEL (MACD > 0 at" in ctx._last_reason
    assert "up-cross, no free slot: SBIN" in ctx._last_reason


async def test_a_new_deployment_buys_only_on_an_up_cross(db_session: AsyncSession):
    """No initial seeding: a first run with nothing held buys only stocks
    crossing up on the newest candle -- not SBIN, two candles past its cross."""
    length = len(_solarinds_closes()[0])
    ctx, _, _ = await _setup(db_session, START, sbin_closes=_stale(_sbin_closes(length), 2))
    ctx.state = {}

    await evaluate(ctx)

    assert ctx.state["holdings"] == {}
    assert "bought" not in ctx._last_reason
    assert "seeded" not in ctx.state


async def _instrument(db_session: AsyncSession, symbol: str) -> Instrument:
    return (await db_session.execute(select(Instrument).where(Instrument.symbol == symbol))).scalar_one()


# --------------------------------------------------------- PCR filter (6 Oct) --

async def _pcr(db_session: AsyncSession, mark: datetime, value: float) -> None:
    from app.models.pcr import SOURCE_LIVE, PcrSnapshot

    ist = timezone(timedelta(hours=5, minutes=30))
    db_session.add(PcrSnapshot(underlying="NIFTY", ts=mark.astimezone(timezone.utc), session_date=mark.astimezone(ist).date(),
                               captured_at=mark, source=SOURCE_LIVE, strike_window=40, expiries=[],
                               contracts_expected=100, contracts_with_oi=100, pcr=value))
    await db_session.commit()


async def _pcr_setup(db_session: AsyncSession):
    """SOLARINDS held (bought after its down-cross, so the MACD rule keeps
    it), alone; SBIN crossing up on the newest candle."""
    _, cross = _solarinds_closes()
    ctx, deployment, instruments = await _setup(db_session, START + (cross + 2) * BAR_LENGTH)
    ctx.state["holdings"] = {"SOLARINDS": ctx.state["holdings"]["SOLARINDS"]}
    return ctx, deployment, instruments


async def test_pcr_below_080_sells_everything_and_skips_the_up_cross(db_session: AsyncSession):
    ctx, deployment, _ = await _pcr_setup(db_session)
    await _pcr(db_session, ctx.now - timedelta(minutes=10), 0.79)

    await evaluate(ctx)

    assert ctx.state["holdings"] == {} and ctx.state["pcr_risk_off"] is True
    assert "sold: SOLARINDS (PCR)" in ctx._last_reason
    assert "up-cross, not bought (out on PCR): SBIN" in ctx._last_reason
    assert "PCR 0.79: out until PCR > 0.90" in ctx._last_reason
    await db_session.commit()
    trade = (await db_session.execute(select(PaperNativeTrade).where(PaperNativeTrade.deployment_id == deployment.id))).scalar_one()
    assert trade.exit_reason == "pcr_below_0.80"


async def test_out_on_pcr_it_waits_above_090_then_buys_only_a_fresh_up_cross(db_session: AsyncSession):
    ctx, _, _ = await _pcr_setup(db_session)
    ctx.state = {"holdings": {}, "pcr_risk_off": True}
    await _pcr(db_session, ctx.now - timedelta(minutes=10), 0.85)

    await evaluate(ctx)  # between 0.80 and 0.90: still out
    assert ctx.state["holdings"] == {} and ctx.state["pcr_risk_off"] is True
    assert "up-cross, not bought (out on PCR): SBIN" in ctx._last_reason

    await _pcr(db_session, ctx.now - timedelta(minutes=1), 0.91)
    await evaluate(ctx)  # above 0.90: back in, and SBIN's up-cross is still fresh
    assert "SBIN" in ctx.state["holdings"] and ctx.state["pcr_risk_off"] is False
    assert "PCR 0.91: in the market" in ctx._last_reason


async def test_back_in_on_pcr_a_stale_up_cross_is_not_bought(db_session: AsyncSession):
    length = len(_solarinds_closes()[0])
    ctx, _, _ = await _setup(db_session, START, sbin_closes=_stale(_sbin_closes(length), 2))
    ctx.state = {"holdings": {}, "pcr_risk_off": True}
    await _pcr(db_session, ctx.now - timedelta(minutes=5), 0.95)

    await evaluate(ctx)

    assert ctx.state["holdings"] == {} and ctx.state["pcr_risk_off"] is False  # in again, but no fresh cross to buy
