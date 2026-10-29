"""52 Week High Breakout (native_strategies/week_high_breakout_52.py, 29 Oct):
the 52 Candle Breakout card moved to weekly candles -- 500 NSE stocks, a
finished week closing above the highest high of the 52 before it is bought
at 09:16 on the next week's first trading day (strongest first, up to 10
stocks), and sold at 09:16 on the trading day after a finished week closes
below the stock's own 10-week MA of (O+H+L+C)/4. Version 1's 15-minute
holdings are sold at the first 09:16 after the switch."""

import uuid
from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.week_high_breakout_52 as wk
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

STOCKS = wk.STOCK_UNIVERSE[:12]
CASH = 2_000_000.0
FIRST = date(2026, 10, 26) - timedelta(weeks=60)  # a Monday: 60 weeks of daily candles before the week of 26 Oct


def at(month: int, day: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, month, day, hh, mm, ss, tzinfo=IST)


def trading_days(first: date, last: date):
    d = first
    while d <= last:
        if wk._is_trading_day(d):
            yield d
        d += timedelta(days=1)


class Market:
    def __init__(self, db):
        self.db = db

    async def build(self) -> "Market":
        db = self.db
        user = User(email=f"wk_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="WK")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="52 Week High Breakout", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=2, timeframe="1wk", instrument_ids=[], parameters={},
                                  python_code="#", position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=user.id, name="WK", cash=CASH, initial_capital=CASH)
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
        return self

    async def day(self, symbol: str, d: date, o: float, h: float, lo: float, c: float) -> None:
        """A daily candle, stamped as Kite's are: the day's midnight IST."""
        self.db.add(OhlcvCandle(instrument_id=self.stocks[symbol].id, timeframe="1d",
                                ts=datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc),
                                open=o, high=h, low=lo, close=c, volume=1000, source="test"))
        await self.db.commit()

    async def history(self, last: date = date(2026, 10, 29), symbols=STOCKS) -> None:
        """Flat days from FIRST to `last`: open 99, high 100, low 98, close 99 -- (O+H+L+C)/4 = 99."""
        for symbol in symbols:
            self.db.add_all([
                OhlcvCandle(instrument_id=self.stocks[symbol].id, timeframe="1d",
                            ts=datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc),
                            open=99.0, high=100.0, low=98.0, close=99.0, volume=1000, source="test")
                for d in trading_days(FIRST, last)
            ])
        await self.db.commit()

    def prices(self, now: datetime, **by_symbol: float) -> None:
        for symbol, instrument in self.stocks.items():
            tick_engine.set_real_price(instrument.id, by_symbol.get(symbol, 99.0), source="test")
            tick_engine._real_price_at[instrument.id] = now

    async def check(self, now: datetime, **prices: float) -> NativeContext:
        self.prices(now, **prices)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await wk.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session):
    m = await Market(db_session).build()
    yield m
    tick_engine.forget([i.id for i in m.stocks.values()])


def test_the_rules():
    days = [{"day": d, "open": 100 + k, "high": 101 + k, "low": 99 + k, "close": 100.5 + k}
            for k, d in enumerate(trading_days(date(2026, 9, 30), date(2026, 10, 9)))]  # Wed 30 Sep .. Fri 9 Oct, 2 Oct a holiday
    w = wk.weeks(days)
    assert [(b["week"], b["last_day"]) for b in w] == [(date(2026, 9, 28), date(2026, 10, 1)), (date(2026, 10, 5), date(2026, 10, 9))]
    assert (w[0]["open"], w[0]["high"], w[0]["low"], w[0]["close"]) == (100, 102, 99, 101.5)
    assert wk._finished(w, date(2026, 10, 12)) and not wk._finished(w[:1], date(2026, 10, 12))
    assert wk._finished(w[:1], date(2026, 10, 5))  # Thu 1 Oct closed the week: Fri 2 Oct was a holiday
    assert not wk._finished(w[:1] + [{**w[1], "last_day": date(2026, 10, 8)}], date(2026, 10, 12))  # Friday's candle not in
    assert wk.first_trading_day(date(2026, 1, 26)) == date(2026, 1, 27)  # Republic Day Monday
    assert wk.last_trading_day_before(date(2026, 10, 5)) == date(2026, 10, 1)
    assert wk.finished_before(at(10, 30, 15, 29)) == date(2026, 10, 26) and wk.finished_before(at(10, 30, 15, 30)) == date(2026, 11, 2)
    assert wk.finished_before(at(10, 1, 15, 30)) == date(2026, 10, 5)  # Thu 1 Oct: Friday a holiday
    assert wk.finished_before(at(11, 1, 12, 0)) == date(2026, 11, 2)  # a Sunday
    assert wk._next_ranking(at(10, 29, 10, 0)) == "Mon 02-Nov 09:16" and wk._next_ranking(at(11, 2, 9, 15)) == "Mon 02-Nov 09:16"
    assert wk._next_ranking(at(11, 2, 9, 16)) == "Mon 09-Nov 09:16" and wk._next_ranking(at(1, 24, 12, 0)) == "Tue 27-Jan 09:16"
    flat = [{"open": 99.0, "high": 100.0, "low": 98.0, "close": 99.0}] * 52
    assert wk.breakout(flat + [{"open": 99, "high": 104, "low": 99, "close": 103}]) == pytest.approx(3.0)
    assert wk.breakout(flat[1:] + [{"open": 99, "high": 104, "low": 99, "close": 103}]) is None  # 52 weeks needed before it
    assert len(wk.STOCK_UNIVERSE) == len(set(wk.STOCK_UNIVERSE)) == 500


async def test_a_weekly_breakout_is_bought_at_0916_on_the_next_weeks_first_trading_day(market):
    a = STOCKS[0]
    await market.history()
    await market.day(a, date(2026, 10, 30), 99, 104, 99, 103)  # the week of 26 Oct closes 3% over its 52-week high
    await market.day(a, date(2026, 11, 2), 103, 120, 103, 119)  # this week's own candle never counts
    early = await market.check(at(11, 2, 9, 15, 30), **{a: 103.5})
    assert early.state["holdings"] == {} and early._wake_at == at(11, 2, 9, 16).astimezone(timezone.utc)
    assert "next ranking Mon 02-Nov 09:16" in early._last_reason

    ctx = await market.check(at(11, 2, 9, 16, 5), **{a: 103.5})
    leg = ctx.state["holdings"][a]
    assert leg["quantity"] == float(int(CASH / 10 / 103.5)) and leg["entry_price"] == 103.5 and leg["breakout_pct"] == 3.0
    assert leg["breakout_week"] == "2026-10-26" and ctx._last_signal == "BUY"
    assert f"{a} @ 103.50 (+3.00% over its 52-week high)" in ctx._last_reason and "next ranking Mon 09-Nov 09:16" in ctx._last_reason
    assert market.portfolio.cash == pytest.approx(CASH - leg["quantity"] * 103.5)


async def test_a_breakout_not_bought_on_the_weeks_first_trading_day_is_passed_over(market):
    a = STOCKS[0]
    await market.history()
    await market.check(at(10, 30, 16, 0))  # the card has run before
    await market.day(a, date(2026, 10, 30), 99, 104, 99, 103)
    ctx = await market.check(at(11, 3, 9, 20), **{a: 103.5})
    assert ctx.state["holdings"] == {} and ctx._last_action == "skipped"


async def test_a_cards_first_run_ranks_last_weeks_breakouts_that_day_the_switch(market):
    a, b, c = STOCKS[:3]
    await market.history(last=date(2026, 10, 22))
    for symbol in STOCKS:
        if symbol not in (a, b):
            await market.day(symbol, date(2026, 10, 23), 99, 100, 98, 99)
    await market.day(a, date(2026, 10, 23), 99, 103, 99, 102)  # the week ending Fri 23 Oct: 2% over the 52-week high
    await market.day(b, date(2026, 10, 23), 99, 106, 99, 105)  # 5%
    market.deployment.state = {"holdings": {  # version 1's, bought on a 15-minute breakout
        c: {"instrument_id": str(market.stocks[c].id), "quantity": 100.0, "entry_price": 100.0,
            "opened_at": "2026-10-28T05:00:00+00:00", "breakout_pct": 1.0, "breakout_candle": "2026-10-28T04:45:00+00:00"}}}
    early = await market.check(at(10, 29, 8, 50), **{a: 102.5, b: 105.5, c: 101.0})
    assert early.state["ranking"]["day"] == "2026-10-29" and "next ranking Thu 29-Oct 09:16" in early._last_reason
    assert f"{c} sells at 09:16 today" in early._last_reason

    ctx = await market.check(at(10, 29, 9, 16, 5), **{a: 102.5, b: 105.5, c: 101.0})
    [trade] = await market.trades()
    assert trade.exit_reason == "switch_to_weekly" and trade.pnl == pytest.approx(100.0)
    assert set(ctx.state["holdings"]) == {a, b} and ctx.state["holdings"][b]["breakout_week"] == "2026-10-19"
    assert ctx._last_reason.index(f"bought {b} @ 105.50 (+5.00%") < ctx._last_reason.index(f"{a} @ 102.50 (+2.00%")
    assert "next ranking Mon 02-Nov 09:16" in ctx._last_reason

    later = await market.check(at(10, 30, 9, 16, 5), **{a: 102.5, b: 105.5, c: 101.0})  # once: not again the next day
    assert set(later.state["holdings"]) == {a, b} and later.state["ranking"]["day"] == "2026-10-29"


async def test_the_strongest_breakouts_fill_the_10_slots(market):
    await market.history()
    for k, symbol in enumerate(STOCKS):  # closes 1% .. 12% over the high
        close = 100 * (1 + (k + 1) / 100)
        await market.day(symbol, date(2026, 10, 30), 99, close + 1, 99, close)
    ctx = await market.check(at(11, 2, 9, 16, 5))
    assert set(ctx.state["holdings"]) == set(STOCKS[2:])  # the 10 strongest
    assert f"no slot for 2 more breakout(s): {STOCKS[1]}, {STOCKS[0]}" in ctx._last_reason
    again = await market.check(at(11, 2, 9, 30))  # the week is ranked once
    assert set(again.state["holdings"]) == set(STOCKS[2:]) and "no slot" not in again._last_reason


async def test_a_stock_whose_week_is_not_in_yet_is_looked_at_again_5_minutes_later(market):
    a, b = STOCKS[:2]
    await market.history(symbols=[a])
    await market.history(last=date(2026, 10, 29), symbols=[b])
    await market.day(a, date(2026, 10, 30), 99, 104, 99, 103)
    first = await market.check(at(11, 2, 9, 16, 5), **{a: 103.5, b: 106.0})
    assert set(first.state["holdings"]) == {a} and "1 stock(s)' last week not in yet" in first._last_reason
    assert first._wake_at == at(11, 2, 9, 21, 5).astimezone(timezone.utc)

    await market.day(b, date(2026, 10, 30), 99, 106, 99, 105)  # Friday's candle comes in
    soon = await market.check(at(11, 2, 9, 18), **{a: 103.5, b: 106.0})
    assert set(soon.state["holdings"]) == {a}  # not looked at again yet
    later = await market.check(at(11, 2, 9, 21, 10), **{a: 103.5, b: 106.0})
    assert set(later.state["holdings"]) == {a, b} and later.state["holdings"][b]["breakout_pct"] == 5.0
    assert "not in yet" not in later._last_reason


async def test_a_finished_week_below_its_10_week_ma_sells_it_at_0916_the_next_trading_day(market):
    a = STOCKS[0]
    await market.history()
    await market.day(a, date(2026, 10, 30), 99, 104, 99, 103)
    bought = await market.check(at(11, 2, 9, 16, 5), **{a: 103.5})
    quantity = bought.state["holdings"][a]["quantity"]

    for d, close in ((date(2026, 11, 2), 100.0), (date(2026, 11, 3), 98.0), (date(2026, 11, 4), 97.0), (date(2026, 11, 5), 96.5)):
        await market.day(a, d, 103.5, 104, 95, close)
    midweek = await market.check(at(11, 6, 10, 0), **{a: 96.0})  # under the MA, but the week is still running
    assert a in midweek.state["holdings"] and await market.trades() == []

    # The week of 2 Nov: open 103.5, high 104, low 95, close 96 -- (O+H+L+C)/4 = 99.625;
    # its 10-week MA (8 flat weeks at 99, the breakout week at 101, this one) = 99.2625.
    await market.day(a, date(2026, 11, 6), 96.5, 97, 95, 96.0)
    evening = await market.check(at(11, 6, 15, 40), **{a: 96.0})
    assert a in evening.state["holdings"]
    assert f"{a} sells at 09:16 on the next trading day (week of 02-Nov closed 96.00 < its 10-week MA 99.26)" in evening._last_reason
    before = await market.check(at(11, 9, 9, 15, 30), **{a: 95.0})
    assert a in before.state["holdings"] and f"{a} sells at 09:16 today" in before._last_reason

    out = await market.check(at(11, 9, 9, 16, 5), **{a: 95.5})
    [trade] = await market.trades()
    assert out.state["holdings"] == {} and trade.exit_reason == "ma_exit" and out._last_signal == "SELL"
    assert trade.pnl == pytest.approx((95.5 - 103.5) * quantity)
    assert market.portfolio.cash == pytest.approx(CASH + trade.pnl)


async def test_a_week_still_over_its_ma_is_held(market):
    a = STOCKS[0]
    await market.history()
    await market.day(a, date(2026, 10, 30), 99, 104, 99, 103)
    await market.check(at(11, 2, 9, 16, 5), **{a: 103.5})
    for d in trading_days(date(2026, 11, 2), date(2026, 11, 6)):
        await market.day(a, d, 103.5, 106, 103, 105)
    ctx = await market.check(at(11, 9, 9, 16, 5), **{a: 105.0})
    assert a in ctx.state["holdings"] and ctx._last_action == "hold" and await market.trades() == []


async def test_version_1s_15_minute_holdings_are_sold_at_the_first_0916_after_the_switch(market):
    a, b = STOCKS[:2]
    await market.history()
    market.deployment.state = {"holdings": {
        s: {"instrument_id": str(market.stocks[s].id), "quantity": 100.0, "entry_price": 100.0,
            "opened_at": "2026-10-28T05:00:00+00:00", "breakout_pct": 1.0, "breakout_candle": "2026-10-28T04:45:00+00:00"}
        for s in (a, b)
    }}
    evening = await market.check(at(10, 29, 16, 0), **{a: 101.0, b: 98.0})
    assert set(evening.state["holdings"]) == {a, b}
    assert f"{a} sells at 09:16 on the next trading day (bought on 15-min candles, the strategy now on weekly ones)" in evening._last_reason

    out = await market.check(at(10, 30, 9, 16, 5), **{a: 101.0, b: 98.0})
    trades = await market.trades()
    assert out.state["holdings"] == {} and {t.exit_reason for t in trades} == {"switch_to_weekly"}
    assert sorted(t.pnl for t in trades) == pytest.approx([-200.0, 100.0])
    assert out.state["ranking"]["day"] == "2026-10-30" and len(out.state["ranking"]["decided"]) == 12  # the first run's: no breakouts


async def test_stop_and_exit_sells_every_holding(market):
    await market.history()
    for symbol in STOCKS[:3]:
        await market.day(symbol, date(2026, 10, 30), 99, 104, 99, 103)
    await market.check(at(11, 2, 9, 16, 5))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    out = await market.check(at(11, 2, 18, 0))
    assert out.state["holdings"] == {} and [t.exit_reason for t in await market.trades()] == ["manual"] * 3
