"""RS Rotation 15 MIN (native_strategies/nifty_rs_rotation_15min.py): at each
15-minute close, rank on live prices over the stored candles before; buy the
top 10 by rank, sell below rank 20, hold overnight."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_rs_rotation_15min as rot
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from app.services.strategy.native_strategies.nifty_rs_rotation import _latest_rs_value

SYMBOLS = [f"S{i:02d}" for i in range(25)]
DAY = datetime(2026, 10, 1, tzinfo=IST)  # Thursday; 30 Sep before it is a session too


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return DAY.replace(hour=hh, minute=mm, second=ss)


def test_decisions_are_on_each_close_from_0930_to_1515_and_only_just_after_it():
    assert rot.just_closed_bar(at(9, 29, 59)) is None
    assert rot.just_closed_bar(at(9, 30, 3)) == at(9, 15)
    assert rot.just_closed_bar(at(9, 32, 10)) is None  # missed: not traded late
    assert rot.just_closed_bar(at(15, 15, 3)) == at(15, 0)
    assert rot.just_closed_bar(at(15, 30, 3)) is None  # the last candle closes with the market
    assert rot.next_close(at(9, 30, 3)) == at(9, 45)
    assert rot.next_close(at(15, 15, 3)) == datetime(2026, 10, 5, 9, 30, tzinfo=IST)  # 2 Oct holiday, then the weekend


def test_the_bars_before_the_first_close_are_the_previous_sessions_last():
    bars = rot.bars_before(at(9, 15), 9)
    assert bars[0] == datetime(2026, 9, 30, 13, 15, tzinfo=IST) and bars[-1] == datetime(2026, 9, 30, 15, 15, tzinfo=IST)
    assert len(set(bars)) == 9


def test_rs_value_is_the_weekly_strategys_formula():
    stock = [100.0, 101.0, 99.5, 102.0, 103.5, 101.0, 104.0, 105.5, 104.0, 107.0]
    bench = [20000.0, 20050.0, 19980.0, 20100.0, 20120.0, 20080.0, 20150.0, 20200.0, 20170.0, 20250.0]
    assert rot.rs_value(stock, bench) == pytest.approx(_latest_rs_value(stock, bench))
    assert rot.rs_value(stock[:-1] + [None], bench) is None  # a missing bar: not ranked


async def _setup(db, monkeypatch):
    monkeypatch.setattr(rot, "STOCK_UNIVERSE", SYMBOLS)
    user = User(email=f"rot_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="Rotation")
    db.add(user)
    await db.flush()
    strategy = Strategy(name="RS Rotation 15 MIN", owner_id=user.id, code_type="native")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                              python_code="#", position_sizing={}, risk_rules={})
    portfolio = PaperPortfolio(user_id=user.id, name="Rotation", cash=1_000_000.0, initial_capital=1_000_000.0)
    db.add_all([version, portfolio])
    await db.flush()
    deployment = PaperNativeDeployment(portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                       status=DeploymentStatus.ACTIVE.value, state=None)
    db.add(deployment)

    instruments = {}
    for symbol in SYMBOLS + ["NIFTY 50"]:
        inst = Instrument(exchange="NSE", symbol=symbol, name=symbol, instrument_type="index" if symbol == "NIFTY 50" else "equity",
                          data_source="zerodha_kite", external_ref=symbol)
        db.add(inst)
        await db.flush()
        instruments[symbol] = inst
        flat = 20000.0 if symbol == "NIFTY 50" else 100.0
        for slot in rot.bars_before(at(9, 15), 9):  # the previous session's last nine candles, stored
            db.add(OhlcvCandle(instrument_id=inst.id, timeframe="15m", ts=slot.astimezone(timezone.utc),
                               open=flat, high=flat, low=flat, close=flat, volume=1000.0, source="test"))
    await db.commit()
    return deployment, portfolio, instruments


def _live(instruments: dict, prices: dict) -> None:
    for symbol, price in prices.items():
        tick_engine.set_real_price(instruments[symbol].id, price, source="test")


async def test_first_close_buys_the_top_ten_by_rank_then_rotates_below_rank_20(db_session, monkeypatch):
    deployment, portfolio, instruments = await _setup(db_session, monkeypatch)
    first = {s: 100.0 + i for i, s in enumerate(SYMBOLS)} | {"NIFTY 50": 20000.0}  # S24 strongest ... S00 weakest
    _live(instruments, first)
    state: dict = {}

    ctx = NativeContext(db=db_session, portfolio=portfolio, deployment=deployment, state=state, now=at(9, 30, 3))
    await rot.evaluate(ctx)

    held = ctx.state["holdings"]
    assert sorted(held) == [f"S{i}" for i in range(15, 25)]  # the ten highest-ranked
    assert held["S24"]["rank"] == 1 and held["S15"]["rank"] == 10
    assert held["S24"]["entry_price"] == 124.0 and held["S24"]["quantity"] == float(int(100_000 / 124.0))
    assert ctx._last_signal == "REBALANCE" and "09:30 close: bought: S24 (rank 1)" in ctx._last_reason
    assert ctx._wake_at == at(9, 45, 3)
    assert len(ctx.state["_series"]["bars"]) == rot.RS_WINDOW

    # The same close again, and a later check that missed the next one: nothing new.
    again = NativeContext(db=db_session, portfolio=portfolio, deployment=deployment, state=ctx.state, now=at(9, 30, 40))
    await rot.evaluate(again)
    assert again._last_action == "hold" and again.state["holdings"] == held

    # 09:45: S24 collapses to the bottom (rank 25 > 20): sold. Everything else
    # holds its place; the freed slot goes to the best stock not held (S14).
    second = dict(first, S24=60.0)
    _live(instruments, second)
    ctx2 = NativeContext(db=db_session, portfolio=portfolio, deployment=deployment, state=ctx.state, now=at(9, 45, 3))
    await rot.evaluate(ctx2)

    assert "S24" not in ctx2.state["holdings"] and "S14" in ctx2.state["holdings"]
    assert len(ctx2.state["holdings"]) == 10
    assert "sold: S24 (rank 25)" in ctx2._last_reason and "bought: S14 (rank 10)" in ctx2._last_reason
    await db_session.commit()
    trade = (await db_session.execute(select(PaperNativeTrade))).scalar_one()
    assert trade.exit_reason == "rank_below_20" and trade.legs[0]["exit_price"] == 60.0


async def test_a_holding_between_rank_11_and_20_is_kept(db_session, monkeypatch):
    deployment, portfolio, instruments = await _setup(db_session, monkeypatch)
    first = {s: 100.0 + i for i, s in enumerate(SYMBOLS)} | {"NIFTY 50": 20000.0}
    _live(instruments, first)
    ctx = NativeContext(db=db_session, portfolio=portfolio, deployment=deployment, state={}, now=at(9, 30, 3))
    await rot.evaluate(ctx)

    # S15 (held, 10th) slips to 14th -- S14 now ranks 10th -- inside the
    # buffer, so it stays, and with ten held nothing is bought.
    second = dict(first, S15=111.0)
    _live(instruments, second)
    ctx2 = NativeContext(db=db_session, portfolio=portfolio, deployment=deployment, state=ctx.state, now=at(9, 45, 3))
    await rot.evaluate(ctx2)

    assert "S15" in ctx2.state["holdings"] and "S14" not in ctx2.state["holdings"] and len(ctx2.state["holdings"]) == 10
    assert "sold: none" in ctx2._last_reason and "bought: none" in ctx2._last_reason
    series = ctx2.state["_series"]["closes"]
    scores = sorted(((rot.rs_value(series[s], series["NIFTY 50"]), s) for s in SYMBOLS), reverse=True)
    assert [s for _, s in scores].index("S15") + 1 == 14


async def test_the_rolling_closes_stay_off_the_card(db_session, monkeypatch):
    from app.api.v1.endpoints.paper_native_trading import _public_state

    assert _public_state({"holdings": {"A": 1}, "_series": {"bars": []}}) == {"holdings": {"A": 1}}
    assert _public_state(None) is None


async def test_no_nifty_price_waits_and_tries_again_within_the_window(db_session, monkeypatch):
    deployment, portfolio, instruments = await _setup(db_session, monkeypatch)
    _live(instruments, {s: 100.0 + i for i, s in enumerate(SYMBOLS)})
    monkeypatch.setattr(tick_engine, "get_current_price", lambda instrument_id: None if instrument_id == instruments["NIFTY 50"].id else 100.0)

    async def no_stored_close(self, instrument_id):
        return None

    monkeypatch.setattr(NativeContext, "_stored_close", no_stored_close)
    ctx = NativeContext(db=db_session, portfolio=portfolio, deployment=deployment, state={}, now=at(9, 30, 3))
    await rot.evaluate(ctx)

    assert ctx._last_action == "skipped" and "no live NIFTY 50 price" in ctx._last_reason
    assert "_series" not in ctx.state and ctx.state.get("holdings") is None
    assert ctx.now + timedelta(minutes=1) - at(9, 30) <= rot.DECIDE_WITHIN  # the next tick still falls inside the window
