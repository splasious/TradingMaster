"""52 Week High Breakout (native_strategies/week_high_breakout_52.py; weekly
29 Oct, the Friday 3 PM check 30 Oct): 500 NSE stocks on weekly candles,
checked once a week -- Friday, or the trading day before when Friday is an
NSE holiday -- at 15:00, this week's candle its stored days plus today's
from Kite's quote, its close the live price. First the stocks held whose
week closes below their 10-week MA of (O+H+L+C)/4 are sold, then the stocks
whose price is above the highest high of the 52 weeks before are bought,
strongest first, up to 10."""

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
FRI = date(2026, 11, 6)  # the check day of the week of 2 Nov
FIRST = date(2026, 11, 2) - timedelta(weeks=60)  # a Monday: 60 weeks of daily candles before it


def at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=IST)


def trading_days(first: date, last: date):
    d = first
    while d <= last:
        if wk._is_trading_day(d):
            yield d
        d += timedelta(days=1)


class Market:
    def __init__(self, db, monkeypatch):
        self.db, self.monkeypatch = db, monkeypatch
        self.quotes: dict[str, tuple[float, float, float, float]] = {}  # symbol -> today's open, high, low, last price
        self.logged_in = True

    async def build(self) -> "Market":
        db = self.db
        user = User(email=f"wk_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="WK")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="52 Week High Breakout", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=3, timeframe="1wk", instrument_ids=[], parameters={},
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

        async def creds(db):
            return {"api_key": "k", "access_token": "t"} if self.logged_in else None

        async def quotes(broker, keys):
            out = {}
            for key in keys:
                q = self.quotes.get(key.split(":", 1)[1])
                if q is not None:
                    out[key] = {"last_price": q[3], "ohlc": {"open": q[0], "high": q[1], "low": q[2], "close": 99.0}}
            return out, None

        self.monkeypatch.setattr(wk, "find_connected_zerodha_credentials", creds)
        self.monkeypatch.setattr(wk, "kite_quotes", quotes)
        return self

    async def day(self, symbol: str, d: date, o: float, h: float, lo: float, c: float) -> None:
        """A daily candle, stamped as Kite's are: the day's midnight IST."""
        self.db.add(OhlcvCandle(instrument_id=self.stocks[symbol].id, timeframe="1d",
                                ts=datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc),
                                open=o, high=h, low=lo, close=c, volume=1000, source="test"))
        await self.db.commit()

    async def history(self, last: date = date(2026, 11, 5), symbols=STOCKS, first: date = FIRST) -> None:
        """Flat days from `first` to `last`: open 99, high 100, low 98, close 99 -- (O+H+L+C)/4 = 99."""
        for symbol in symbols:
            self.db.add_all([
                OhlcvCandle(instrument_id=self.stocks[symbol].id, timeframe="1d",
                            ts=datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc),
                            open=99.0, high=100.0, low=98.0, close=99.0, volume=1000, source="test")
                for d in trading_days(first, last)
            ])
        await self.db.commit()

    def flat_quotes(self, **by_symbol: tuple[float, float, float, float]) -> None:
        self.quotes = {s: by_symbol.get(s, (99.0, 100.0, 98.0, 99.0)) for s in STOCKS}

    async def check(self, now: datetime, **prices: float) -> NativeContext:
        for symbol, instrument in self.stocks.items():
            tick_engine.set_real_price(instrument.id, prices.get(symbol, 99.0), source="test")
            tick_engine._real_price_at[instrument.id] = now
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await wk.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    def hold(self, symbol: str, entry: float, opened: datetime, quantity: float = 100.0) -> dict:
        return {"instrument_id": str(self.stocks[symbol].id), "quantity": quantity, "entry_price": entry,
                "opened_at": opened.astimezone(timezone.utc).isoformat(), "breakout_pct": 3.0, "breakout_week": "2026-10-26"}

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session, monkeypatch).build()
    yield m
    tick_engine.forget([i.id for i in m.stocks.values()])


def test_the_rules():
    assert wk.check_day(date(2026, 11, 2)) == FRI
    assert wk.check_day(date(2026, 9, 28)) == date(2026, 10, 1)  # Fri 2 Oct, Gandhi Jayanti: Thursday
    assert wk.check_day(date(2026, 12, 21)) == date(2026, 12, 24)  # Christmas Friday
    assert wk._next_check(at(FRI, 14, 59)) == "Fri 06-Nov 15:00" and wk._next_check(at(FRI, 15, 0)) == "Fri 13-Nov 15:00"
    assert wk._next_check(at(date(2026, 9, 30), 10, 0)) == "Thu 01-Oct 15:00"
    days = [{"day": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5} for d in trading_days(date(2026, 10, 26), date(2026, 11, 5))]
    today = {"day": FRI, "open": 100.0, "high": 106.0, "low": 95.0, "close": 104.0}
    w = wk.week_so_far(days, today, date(2026, 11, 2), FRI)
    assert [(b["week"], b["last_day"]) for b in w] == [(date(2026, 10, 26), date(2026, 10, 30)), (date(2026, 11, 2), FRI)]
    assert (w[-1]["open"], w[-1]["high"], w[-1]["low"], w[-1]["close"]) == (100.0, 106.0, 95.0, 104.0)
    assert wk.week_so_far(days, None, date(2026, 11, 2), FRI) is None  # no quote
    assert wk.week_so_far([d for d in days if d["day"] != date(2026, 11, 4)], today, date(2026, 11, 2), FRI) is None  # Wednesday not stored
    assert wk.week_so_far([d for d in days if d["day"] != date(2026, 10, 30)], today, date(2026, 11, 2), FRI) is None  # last Friday not stored
    flat = [{"open": 99.0, "high": 100.0, "low": 98.0, "close": 99.0}] * 52
    assert wk.breakout(flat + [{"open": 99, "high": 104, "low": 99, "close": 103}]) == pytest.approx(3.0)
    assert wk.breakout(flat[1:] + [{"open": 99, "high": 104, "low": 99, "close": 103}]) is None  # 52 weeks needed before it
    assert len(wk.STOCK_UNIVERSE) == len(set(wk.STOCK_UNIVERSE)) == 500


async def test_nothing_trades_before_fridays_3pm(market):
    a = STOCKS[0]
    await market.history()
    market.flat_quotes(**{a: (99.0, 104.0, 99.0, 103.0)})
    thursday = await market.check(at(date(2026, 11, 5), 15, 0, 5))
    assert thursday.state["holdings"] == {} and "next check Fri 06-Nov 15:00" in thursday._last_reason
    early = await market.check(at(FRI, 14, 59))
    assert early.state["holdings"] == {} and early._wake_at == at(FRI, 15, 0).astimezone(timezone.utc)


async def test_a_52_week_high_at_3pm_is_bought_at_the_live_price(market):
    a = STOCKS[0]
    await market.history()
    market.flat_quotes(**{a: (99.0, 104.0, 99.0, 103.0)})  # 3% over the 52-week high of 100
    ctx = await market.check(at(FRI, 15, 0, 5))
    leg = ctx.state["holdings"][a]
    assert leg["quantity"] == float(int(CASH / 10 / 103.0)) and leg["entry_price"] == 103.0 and leg["breakout_pct"] == 3.0
    assert leg["breakout_week"] == "2026-11-02" and ctx._last_signal == "BUY"
    assert f"{a} @ 103.00 (+3.00% over its 52-week high)" in ctx._last_reason and "next check Fri 13-Nov 15:00" in ctx._last_reason
    assert ctx.state["check"]["waiting"] == 0 and len(ctx.state["check"]["decided"]) == 12
    again = await market.check(at(FRI, 15, 10))  # once a week
    assert set(again.state["holdings"]) == {a} and again._last_action == "hold"


async def test_the_strongest_breakouts_fill_the_10_slots(market):
    await market.history()
    market.flat_quotes(**{s: (99.0, 100 * (1 + (k + 1) / 100) + 1, 99.0, 100 * (1 + (k + 1) / 100)) for k, s in enumerate(STOCKS)})
    ctx = await market.check(at(FRI, 15, 0, 5))
    assert set(ctx.state["holdings"]) == set(STOCKS[2:])  # the 10 strongest
    assert f"no slot for 2 more breakout(s): {STOCKS[1]}, {STOCKS[0]}" in ctx._last_reason


async def test_a_holding_whose_week_closes_under_its_10_week_ma_at_3pm_is_sold_first(market):
    a, b, c = STOCKS[:3]
    await market.history(last=date(2026, 11, 1))
    for d in trading_days(date(2026, 11, 2), date(2026, 11, 5)):
        await market.day(a, d, 99, 100, 95, 96)
        for symbol in STOCKS[1:]:
            await market.day(symbol, d, 99, 100, 98, 99)
    market.deployment.state = {"holdings": {a: market.hold(a, 103.0, at(date(2026, 10, 30), 15, 0)),
                                            b: market.hold(b, 99.0, at(date(2026, 10, 30), 15, 0))}}
    # The week of 2 Nov at 15:00 for a: open 99, high 100, low 95, close 96 -- (O+H+L+C)/4 = 97.5;
    # its 10-week MA (9 flat weeks at 99 and this one) = 98.85. b: flat, at its MA -- held.
    market.flat_quotes(**{a: (96.0, 97.0, 95.5, 96.0), c: (99.0, 104.0, 99.0, 103.0)})
    ctx = await market.check(at(FRI, 15, 0, 5))
    [trade] = await market.trades()
    assert trade.exit_reason == "ma_exit" and trade.pnl == pytest.approx((96.0 - 103.0) * 100)
    assert f"sold {a} @ 96.00 (week to 06-Nov at 15:00: 96.00 < its 10-week MA 98.85" in ctx._last_reason
    assert set(ctx.state["holdings"]) == {b, c}  # b held; c bought after the sale


async def test_a_stock_bought_at_this_check_is_checked_from_the_next(market):
    a = STOCKS[0]
    await market.history()
    market.deployment.state = {"holdings": {a: market.hold(a, 103.0, at(FRI, 15, 0, 2))}}
    market.flat_quotes(**{a: (99.0, 100.0, 90.0, 91.0)})  # under its MA, but bought at 15:00:02 today
    ctx = await market.check(at(FRI, 15, 1, 5))
    assert a in ctx.state["holdings"] and await market.trades() == []


async def test_a_stock_without_a_quote_or_a_day_is_looked_at_again_a_minute_later(market):
    a, b = STOCKS[:2]
    await market.history(symbols=STOCKS[1:])
    await market.history(last=date(2026, 11, 3), symbols=[a])  # Wednesday 4 Nov not in yet
    market.flat_quotes(**{a: (99.0, 106.0, 99.0, 105.0)})
    del market.quotes[b]
    first = await market.check(at(FRI, 15, 0, 5))
    assert first.state["holdings"] == {} and first.state["check"]["waiting"] == 2
    assert "2 stock(s) without today's quote or a stored day" in first._last_reason
    assert first._wake_at == at(FRI, 15, 1, 5).astimezone(timezone.utc)

    await market.day(a, date(2026, 11, 4), 99, 100, 98, 99)
    await market.day(a, date(2026, 11, 5), 99, 100, 98, 99)
    market.quotes[b] = (99.0, 104.0, 99.0, 103.0)
    soon = await market.check(at(FRI, 15, 0, 40))
    assert soon.state["holdings"] == {}  # not looked at again yet
    later = await market.check(at(FRI, 15, 1, 10))
    assert set(later.state["holdings"]) == {a, b} and later.state["check"]["waiting"] == 0


async def test_a_friday_holiday_moves_the_check_to_thursday(market):
    a = STOCKS[0]
    await market.history(last=date(2026, 9, 30), first=date(2026, 9, 28) - timedelta(weeks=60))
    market.flat_quotes(**{a: (99.0, 104.0, 99.0, 103.0)})
    ctx = await market.check(at(date(2026, 10, 1), 15, 0, 5))  # Thu 1 Oct: Fri 2 Oct is Gandhi Jayanti
    assert set(ctx.state["holdings"]) == {a} and ctx.state["holdings"][a]["breakout_week"] == "2026-09-28"
    assert "next check Fri 09-Oct 15:00" in ctx._last_reason


async def test_without_a_kite_login_nothing_trades_and_it_says_why(market):
    await market.history()
    market.logged_in = False
    ctx = await market.check(at(FRI, 15, 0, 5))
    assert ctx.state["holdings"] == {} and "Zerodha isn't logged in" in ctx._last_reason and ctx.state["check"]["waiting"] == 12


async def test_stop_and_exit_sells_every_holding(market):
    await market.history()
    market.deployment.state = {"holdings": {s: market.hold(s, 100.0, at(date(2026, 10, 30), 15, 0)) for s in STOCKS[:3]},
                               "force_exit": True}
    out = await market.check(at(FRI, 18, 0))
    assert out.state["holdings"] == {} and [t.exit_reason for t in await market.trades()] == ["manual"] * 3
