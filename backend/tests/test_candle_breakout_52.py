"""52 Candle Breakout 15 MIN (native_strategies/candle_breakout_52_15min.py,
28 Oct): 500 NSE stocks on 15-minute candles -- bought when a candle closes
above the highest high of the 52 before it (ranked 4 minutes after the
close, strongest first, up to 10 stocks), sold when a candle closed after
the buy closes below the stock's own 10-candle MA of (O+H+L+C)/4."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.candle_breakout_52_15min as bo
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import (
    DeploymentStatus,
    PaperNativeDeployment,
    PaperNativeTrade,
    PaperPortfolio,
)
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext

STOCKS = bo.STOCK_UNIVERSE[:12]
CASH = 2_000_000.0
LEVEL = 100.0  # every candle before a breakout: open 99, high 100, low 98, close 99 -- (O+H+L+C)/4 = 99


def at(day: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=IST)


def session_starts(day: int, until: datetime | None = None):
    t = at(day, 9, 15)
    while t <= at(day, 15, 15) and (until is None or t < until):
        yield t
        t += timedelta(minutes=15)


class Market:
    def __init__(self, db, monkeypatch):
        self.db, self.monkeypatch, self.demanded = db, monkeypatch, set()

    async def build(self) -> "Market":
        db = self.db
        user = User(email=f"bo_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="BO")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="52 Candle Breakout 15 MIN", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                                  python_code="#", position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=user.id, name="BO", cash=CASH, initial_capital=CASH)
        db.add_all([version, self.portfolio])
        await db.flush()
        self.deployment = PaperNativeDeployment(portfolio_id=self.portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                                status=DeploymentStatus.ACTIVE.value, state=None)
        self.stocks = {
            s: Instrument(exchange="NSE", symbol=s, name=s, instrument_type="equity", data_source="zerodha_kite", external_ref=s)
            for s in STOCKS
        }
        db.add_all([self.deployment, *self.stocks.values()])
        await db.commit()
        await db.refresh(self.deployment)
        self.monkeypatch.setattr(bo, "note_native_candle_demand", lambda instrument_id, tf, now=None: self.demanded.add((instrument_id, tf)))
        return self

    async def candle(self, symbol: str, start: datetime, o: float, h: float, lo: float, c: float) -> None:
        self.db.add(OhlcvCandle(instrument_id=self.stocks[symbol].id, timeframe="15m", ts=start.astimezone(timezone.utc),
                                open=o, high=h, low=lo, close=c, volume=1000, source="test"))
        await self.db.commit()

    async def history(self, until: datetime, symbols=STOCKS) -> None:
        """Flat candles on 26 and 27 Oct and 28 Oct's before `until`."""
        for symbol in symbols:
            for day in (26, 27, 28):
                for start in session_starts(day, until if day == 28 else None):
                    self.db.add(OhlcvCandle(instrument_id=self.stocks[symbol].id, timeframe="15m", ts=start.astimezone(timezone.utc),
                                            open=99.0, high=LEVEL, low=98.0, close=99.0, volume=1000, source="test"))
        await self.db.commit()

    def prices(self, now: datetime, **by_symbol: float) -> None:
        for symbol, instrument in self.stocks.items():
            tick_engine.set_real_price(instrument.id, by_symbol.get(symbol, 99.0), source="test")
            tick_engine._real_price_at[instrument.id] = now

    async def check(self, now: datetime, **prices: float) -> NativeContext:
        self.prices(now, **prices)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await bo.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session, monkeypatch).build()
    yield m
    tick_engine.forget([i.id for i in m.stocks.values()])


def flat(n: int) -> list[dict]:
    return [{"open": 99.0, "high": LEVEL, "low": 98.0, "close": 99.0}] * n


def test_the_rules():
    assert bo.breakout(flat(52) + [{"open": 99, "high": 104, "low": 99, "close": 103}]) == pytest.approx(3.0)
    assert bo.breakout(flat(52) + [{"open": 99, "high": 104, "low": 99, "close": 100}]) is None  # level, not above
    assert bo.breakout(flat(51) + [{"open": 99, "high": 104, "low": 99, "close": 103}]) is None  # 52 needed before it
    assert bo.typical({"open": 99, "high": 100, "low": 98, "close": 99}) == 99
    assert bo.moving_average(flat(9)) is None and bo.moving_average(flat(9) + [{"open": 99, "high": 104, "low": 99, "close": 103}]) == (
        pytest.approx((9 * 99 + 101.25) / 10))
    assert bo.last_close(at(28, 9, 29)) is None and bo.last_close(at(28, 10, 7)) == at(28, 10, 0)
    assert bo.last_close(at(28, 18, 0)) == at(28, 15, 30) and len(bo.STOCK_UNIVERSE) == len(set(bo.STOCK_UNIVERSE)) == 500


async def test_a_breakout_is_bought_four_minutes_after_its_candle_closes(market):
    a = STOCKS[0]
    await market.history(at(28, 10, 0))
    await market.candle(a, at(28, 10, 0), 99, 104, 99, 103)  # 10:00-10:15 closes 3% over the 52-candle high
    early = await market.check(at(28, 10, 16), **{a: 103.5})
    assert early.state["holdings"] == {} and early._wake_at == at(28, 10, 19).astimezone(timezone.utc)

    ctx = await market.check(at(28, 10, 19, 5), **{a: 103.5})
    leg = ctx.state["holdings"][a]
    assert leg["quantity"] == float(int(CASH / 10 / 103.5)) and leg["entry_price"] == 103.5 and leg["breakout_pct"] == 3.0
    assert ctx._last_signal == "BUY" and f"{a} @ 103.50 (+3.00% over its 52-candle high)" in ctx._last_reason
    assert market.portfolio.cash == pytest.approx(CASH - leg["quantity"] * 103.5)
    assert {(i.id, "15m") for i in market.stocks.values()} <= market.demanded  # live: every stock kept synced


async def test_the_strongest_breakouts_fill_the_10_slots(market):
    await market.history(at(28, 10, 0))
    for k, symbol in enumerate(STOCKS):  # closes 1% .. 12% over the high
        await market.candle(symbol, at(28, 10, 0), 99, 115, 99, LEVEL * (1 + (k + 1) / 100))
    ctx = await market.check(at(28, 10, 19, 5))
    assert set(ctx.state["holdings"]) == set(STOCKS[2:])  # the 10 strongest
    assert f"no slot for 2 more breakout(s): {STOCKS[1]}, {STOCKS[0]}" in ctx._last_reason


async def test_a_late_candle_is_bought_if_a_slot_is_free_until_the_next_close(market):
    a, b, c = STOCKS[:3]
    await market.history(at(28, 10, 0))
    await market.candle(a, at(28, 10, 0), 99, 104, 99, 103)
    await market.check(at(28, 10, 19, 5))
    await market.candle(b, at(28, 10, 0), 99, 106, 99, 105)  # its candle came in at 10:22
    late = await market.check(at(28, 10, 22))
    assert set(late.state["holdings"]) == {a, b}
    await market.candle(c, at(28, 10, 0), 99, 106, 99, 105)  # too late: the next candle has closed
    after = await market.check(at(28, 10, 31))
    assert c not in after.state["holdings"]


async def test_the_days_last_candle_is_not_bought(market):
    a = STOCKS[0]
    await market.history(at(28, 15, 15))
    await market.candle(a, at(28, 15, 15), 99, 104, 99, 103)  # 15:15-15:30
    ctx = await market.check(at(28, 15, 34))
    assert ctx.state["holdings"] == {} and "next ranking 09:34 on the next trading day" in ctx._last_reason


async def test_a_close_below_its_10_candle_ma_after_the_buy_sells_it(market):
    a = STOCKS[0]
    await market.history(at(28, 10, 0))
    await market.candle(a, at(28, 10, 0), 99, 104, 99, 103)
    bought = await market.check(at(28, 10, 19, 5), **{a: 103.5})
    quantity = bought.state["holdings"][a]["quantity"]
    held = await market.check(at(28, 10, 25), **{a: 98.0})  # under the MA live, but no candle has closed since the buy
    assert a in held.state["holdings"]

    await market.candle(a, at(28, 10, 15), 99, 99.5, 97, 97.5)  # 10:15-10:30 closes under the MA (98.67)
    out = await market.check(at(28, 10, 30, 20), **{a: 97.8})
    [trade] = await market.trades()
    assert out.state["holdings"] == {} and trade.exit_reason == "ma_exit" and out._last_signal == "SELL"
    assert trade.pnl == pytest.approx((97.8 - 103.5) * quantity)
    assert market.portfolio.cash == pytest.approx(CASH + trade.pnl)


async def test_a_close_below_the_ma_on_the_last_candle_sells_at_the_next_open(market):
    a = STOCKS[0]
    await market.history(at(28, 14, 30))
    await market.candle(a, at(28, 14, 30), 99, 104, 99, 103)
    await market.check(at(28, 14, 49), **{a: 103.0})
    for start in session_starts(28):
        if start > at(28, 14, 30):
            await market.candle(a, start, 103, 103.5, 102, 103)
    held = await market.check(at(28, 15, 31), **{a: 103.0})
    assert a in held.state["holdings"]  # still over its MA at the close

    # Rewrite the day's last candle under the MA: found after the close, sold at the next session's first price.
    last = (await market.db.execute(select(OhlcvCandle).where(
        OhlcvCandle.instrument_id == market.stocks[a].id, OhlcvCandle.ts == at(28, 15, 15).astimezone(timezone.utc)))).scalar_one()
    last.close, last.low = 95.0, 95.0
    await market.db.commit()
    evening = await market.check(at(28, 15, 40), **{a: 95.0})
    assert a in evening.state["holdings"] and f"{a} sells at the open" in evening._last_reason
    morning = await market.check(at(29, 9, 15, 5), **{a: 94.0})
    [trade] = await market.trades()
    assert morning.state["holdings"] == {} and trade.exit_reason == "ma_exit"


async def test_stop_and_exit_sells_every_holding(market):
    await market.history(at(28, 10, 0))
    for symbol in STOCKS[:3]:
        await market.candle(symbol, at(28, 10, 0), 99, 104, 99, 103)
    await market.check(at(28, 10, 19, 5))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    out = await market.check(at(28, 10, 21))
    assert out.state["holdings"] == {} and [t.exit_reason for t in await market.trades()] == ["manual"] * 3


async def test_a_backtest_reads_the_stored_candles_without_asking_for_syncs(market):
    from app.services.backtest.native_runner import BacktestNativeContext

    a = STOCKS[0]
    await market.history(at(28, 10, 0))
    await market.candle(a, at(28, 10, 0), 99, 104, 99, 103)
    market.prices(at(28, 10, 20), **{a: 103.5})
    ctx = BacktestNativeContext(db=market.db, portfolio=market.portfolio, deployment=PaperNativeDeployment(
        portfolio_id=market.portfolio.id, strategy_id=market.deployment.strategy_id, strategy_version_id=market.deployment.strategy_version_id,
        status="active", state={}), state={}, now=at(28, 10, 20).astimezone(timezone.utc))
    await bo.evaluate(ctx)
    assert set(ctx.state["holdings"]) == {a} and market.demanded == set()


def test_the_candle_sync_fetches_nifty_and_fno_before_stocks():
    from app.services.market_data.active_timeframe_sync_scheduler import _sync_order

    nifty = Instrument(exchange="NSE", symbol="NIFTY 50", instrument_type="index")
    future = Instrument(exchange="NFO", symbol="NIFTY26NOVFUT", instrument_type="future")
    stock = Instrument(exchange="NSE", symbol="ABB", instrument_type="equity")
    pairs = [(stock, "15m"), (future, "15m"), (nifty, "15m"), (None, "15m")]
    ordered = [p[0] for p in sorted(pairs, key=lambda p: _sync_order(p[0], p[1]))]
    assert ordered == [None, nifty, future, stock]
