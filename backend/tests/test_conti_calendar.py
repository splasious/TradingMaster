"""Conti Calendar 15 MIN (native_strategies/nifty_conti_calendar.py, 4 Nov):
Conti Straddle's short weekly straddle with the same strike's monthly CE and
PE bought, 16 lots a leg, every fill at bid / ask; held overnight with no
rolls until Rs 50,000 after charges at bid / ask, or 15:00 on the weekly's
expiry day."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_conti_calendar as cal
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data import hours, live_price
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from app.services.paper_trading.trade_record import order_charges

W1, W2, W3 = date(2026, 11, 9), date(2026, 11, 17), date(2026, 11, 23)  # weeklies; 23 Nov is also November's monthly
DEC = date(2026, 12, 29)  # December's monthly
SYMBOLS = {W1: "NIFTY26N09", W2: "NIFTY26N17", W3: "NIFTY26NOV", DEC: "NIFTY26DEC"}
SPREAD = 0.5  # the book's half-spread around each premium in these tests
QTY = 16 * 65.0


def at(day: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=IST)


def test_a_monthly_is_told_by_its_tradingsymbol():
    assert cal.is_monthly("NIFTY26NOV23700CE") and cal.is_monthly("NIFTY26DEC23700PE")
    assert not cal.is_monthly("NIFTY26N1723700CE") and not cal.is_monthly("BANKNIFTY26NOV50000CE") and not cal.is_monthly(None)


def test_closing_now_is_at_the_ask_for_a_sale_and_the_bid_for_a_buy_less_every_charge():
    leg = lambda side, entry: {"side": side, "option_type": "CE", "quantity": QTY, "entry_price": entry}  # noqa: E731
    position = {"legs": {"s": leg("sell", 100.0), "b": leg("buy", 200.0)}, "entry_charges": 300.0}
    net, exit_charges, prices = cal.if_closed_now(position, {"s": (79.5, 80.0), "b": (229.5, 230.0)})
    assert prices == {"s": 80.0, "b": 229.5}
    gross = (100 - 80.0) * QTY + (229.5 - 200) * QTY
    expected_exit = order_charges({"option_type": "CE", "quantity": QTY}, 80.0, True, False) + \
        order_charges({"option_type": "CE", "quantity": QTY}, 229.5, False, False)
    assert exit_charges == pytest.approx(expected_exit) and net == pytest.approx(gross - 300.0 - expected_exit)


class Market:
    def __init__(self, db):
        self.db, self.pcr, self.weeklies, self.no_book = db, 0.9, [W1, W2, W3], set()
        self.premium: dict[tuple[date, float, str], float] = {}

    async def build(self, monkeypatch) -> "Market":
        db = self.db
        user = User(email=f"cal_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="Cal")
        db.add(user)
        await db.flush()
        strategy = Strategy(name="Conti Calendar 15 MIN", owner_id=user.id, code_type="native")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                                  python_code="#", position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=user.id, name="Conti Calendar 15 MIN", cash=2_000_000.0, initial_capital=2_000_000.0)
        db.add_all([version, self.portfolio])
        await db.flush()
        self.deployment = PaperNativeDeployment(portfolio_id=self.portfolio.id, strategy_id=strategy.id,
                                                strategy_version_id=version.id, status=DeploymentStatus.ACTIVE.value, state=None)
        self.nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index",
                                data_source="zerodha_kite", external_ref="NIFTY 50")
        db.add_all([self.deployment, self.nifty])
        await db.flush()
        self.options = [
            Instrument(exchange="NFO", symbol=f"{SYMBOLS[e]}{k}{t}", name="opt", instrument_type="option", data_source="zerodha_kite",
                       external_ref=f"{SYMBOLS[e]}{k}{t}", expiry=e, strike=float(k), option_type=t, lot_size=65,
                       underlying_instrument_id=self.nifty.id)
            for e in SYMBOLS for k in range(23400, 24001, 50) for t in ("CE", "PE")
        ]
        db.add_all(self.options)
        await db.commit()

        async def get_pcr(ctx, *args, **kwargs):
            return self.pcr

        async def expiries(ctx, underlying_id, today, limit=4):
            return [e for e in self.weeklies if e >= today][:limit]

        monkeypatch.setattr(NativeContext, "get_pcr", get_pcr)
        monkeypatch.setattr(NativeContext, "list_weekly_expiries", expiries)
        # Prices and the order book only count while NSE is open (conftest turns that rule off).
        monkeypatch.setattr(live_price, "nse_market_open", hours.nse_market_open)
        return self

    def option(self, expiry: date, strike: float, option_type: str) -> Instrument:
        return next(o for o in self.options if (o.expiry, o.strike, o.option_type) == (expiry, strike, option_type))

    def prices(self, now: datetime, spot: float) -> None:
        tick_engine.set_real_price(self.nifty.id, spot, source="test")
        tick_engine._real_price_at[self.nifty.id] = now
        for o in self.options:
            weeks = max(1, (o.expiry - now.date()).days / 7)
            lead = (spot - o.strike) / 2  # CE - PE = NIFTY - strike
            ltp = self.premium.get((o.expiry, o.strike, o.option_type), 100 * weeks + (lead if o.option_type == "CE" else -lead))
            tick_engine.set_real_price(o.id, ltp, source="test")
            tick_engine._real_price_at[o.id] = now
            if (o.expiry, o.strike, o.option_type) in self.no_book:
                tick_engine._real_bid_ask.pop(o.id, None)
            else:
                tick_engine.set_real_bid_ask(o.id, ltp - SPREAD, ltp + SPREAD, at=now)

    async def check(self, now: datetime, spot: float = 23460.0) -> NativeContext:
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await cal.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def trades(self) -> list[PaperNativeTrade]:
        return (await self.db.execute(select(PaperNativeTrade).order_by(PaperNativeTrade.closed_at))).scalars().all()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market(db_session).build(monkeypatch)
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


async def test_it_opens_at_0930_selling_the_weekly_and_buying_the_monthly_at_one_strike_at_bid_and_ask(market):
    early = await market.check(at(4, 9, 29))
    assert early.state.get("position") is None and "outside entry window (09:30-15:00 IST)" in early._last_reason

    ctx = await market.check(at(4, 9, 30))
    p = ctx.state["position"]
    assert (p["strike"], p["side"], p["weekly_expiry"], p["monthly_expiry"]) == (23450, "call", "2026-11-09", "2026-11-23")
    legs = p["legs"]
    assert {n: (leg["side"], leg["expiry"], leg["option_type"], leg["quantity"]) for n, leg in legs.items()} == {
        "short_ce": ("sell", "2026-11-09", "CE", QTY), "short_pe": ("sell", "2026-11-09", "PE", QTY),
        "long_ce": ("buy", "2026-11-23", "CE", QTY), "long_pe": ("buy", "2026-11-23", "PE", QTY)}
    ltp = {n: tick_engine.get_current_price(uuid.UUID(leg["instrument_id"])) for n, leg in legs.items()}
    assert legs["short_ce"]["entry_price"] == ltp["short_ce"] - SPREAD and legs["short_pe"]["entry_price"] == ltp["short_pe"] - SPREAD
    assert legs["long_ce"]["entry_price"] == ltp["long_ce"] + SPREAD and legs["long_pe"]["entry_price"] == ltp["long_pe"] + SPREAD
    credit = (legs["short_ce"]["entry_price"] + legs["short_pe"]["entry_price"]) * QTY
    debit = (legs["long_ce"]["entry_price"] + legs["long_pe"]["entry_price"]) * QTY
    assert market.portfolio.cash == pytest.approx(2_000_000.0 + credit - debit)
    assert p["entry_charges"] > 0 and "16 lots a leg" in ctx._last_reason and ctx._last_signal == "CALENDAR"


async def test_it_is_held_overnight_and_past_1500_on_a_day_that_isnt_the_weeklys_expiry(market):
    await market.check(at(4, 9, 30))
    for now in (at(4, 15, 0), at(4, 15, 29), at(4, 20, 0), at(5, 9, 0), at(5, 15, 5), at(6, 11, 0)):
        ctx = await market.check(now)
        assert ctx.state["position"] is not None, now
    assert not await market.trades()
    night = await market.check(at(5, 21, 0))
    assert "held overnight" in night._last_reason and "from 09:15" in night._last_reason
    day = await market.check(at(6, 11, 0))
    assert "if closed now at bid/ask after charges" in day._last_reason
    assert day.state["position"]["net_if_closed_now"] < 0  # the spreads and charges paid, nothing earned yet


async def test_it_takes_the_profit_once_closing_now_at_bid_and_ask_would_give_50000_after_charges(market):
    held = (await market.check(at(4, 9, 30))).state["position"]

    def decay_to(each: float) -> None:  # the weekly's two premiums fall; the monthly's are as the day prices them
        market.premium[(W1, 23450.0, "CE")] = market.premium[(W1, 23450.0, "PE")] = each

    market.prices(at(5, 11, 0), 23460.0)  # the next day's book
    longs = {n: tick_engine.get_fresh_bid_ask(uuid.UUID(held["legs"][n]["instrument_id"]), timedelta(minutes=1), at(5, 11, 0))
             for n in ("long_ce", "long_pe")}

    def net(each: float) -> float:
        return cal.if_closed_now(held, {"short_ce": (each - SPREAD, each + SPREAD), "short_pe": (each - SPREAD, each + SPREAD), **longs})[0]

    lo, hi = 0.0, 300.0  # the weekly premium where closing now gives exactly Rs 50,000 lies between
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if net(mid) >= cal.TAKE_PROFIT else (lo, mid)

    decay_to(hi + 0.05)  # just short of Rs 50,000: held
    under = await market.check(at(5, 11, 0))
    assert under.state["position"] is not None and under.state["position"]["net_if_closed_now"] == pytest.approx(net(hi + 0.05), abs=0.01)
    assert under.state["position"]["net_if_closed_now"] < cal.TAKE_PROFIT

    decay_to(lo - 0.05)  # just past it: all four close at bid / ask
    done = await market.check(at(5, 11, 0, 10))
    assert done.state["position"] is None and done._last_signal == "COVER" and "take-profit" in done._last_reason
    [trade] = await market.trades()
    assert trade.exit_reason == "take_profit"
    exits = {(leg["side"], leg["exit_price"]) for leg in trade.legs}
    assert ("short", lo - 0.05 + SPREAD) in exits  # bought back at the ask
    assert ("long", longs["long_ce"][0]) in exits and ("long", longs["long_pe"][0]) in exits  # sold at the bid

    again = await market.check(at(5, 11, 0, 20))  # the next check opens the next one
    assert again.state["position"] is not None and again._last_signal == "CALENDAR"


async def test_on_the_weeklys_expiry_day_all_four_close_at_1500_and_the_next_opens_the_next_day_at_0930(market):
    await market.check(at(6, 9, 30))
    held = await market.check(at(9, 14, 59))
    assert held.state["position"] is not None
    closed = await market.check(at(9, 15, 0))
    assert closed.state["position"] is None and "weekly 09 Nov expiry, 15:00: all four legs closed" in closed._last_reason
    [trade] = await market.trades()
    assert trade.exit_reason == "weekly_expiry_3pm" and len(trade.legs) == 4
    later = await market.check(at(9, 15, 10))
    assert later.state.get("position") is None and "outside entry window" in later._last_reason
    nxt = await market.check(at(11, 9, 30))  # 10 Nov is a holiday
    assert (nxt.state["position"]["weekly_expiry"], nxt.state["position"]["monthly_expiry"]) == ("2026-11-17", "2026-11-23")


async def test_on_an_expiry_day_it_opens_on_next_weeks_and_in_the_months_last_week_buys_next_months(market):
    expiry_day = await market.check(at(9, 9, 30))
    assert expiry_day.state["position"]["weekly_expiry"] == "2026-11-17"
    market.deployment.state = None
    market.weeklies = [W3, DEC]
    last_week = await market.check(at(18, 9, 30))
    p = last_week.state["position"]
    assert (p["weekly_expiry"], p["monthly_expiry"]) == ("2026-11-23", "2026-12-29")
    assert p["legs"]["long_ce"]["expiry"] == "2026-12-29"


async def test_no_bid_or_ask_on_a_leg_means_no_entry_and_no_take_profit_reading(market):
    market.no_book = {(W3, 23450.0, "PE")}
    ctx = await market.check(at(4, 9, 30))
    assert ctx.state.get("position") is None and "no bid/ask for the 23,450 PE monthly" in ctx._last_reason
    market.no_book = set()
    await market.check(at(4, 9, 31))
    market.no_book = {(W1, 23450.0, "CE")}
    held = await market.check(at(4, 10, 0))
    assert held.state["position"] is not None and "no bid/ask for short_ce" in held._last_reason


async def test_a_manual_exit_closes_all_four(market):
    await market.check(at(4, 9, 30))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    ctx = await market.check(at(4, 10, 0))
    assert ctx.state["position"] is None and ctx._last_reason == "manual exit"
    [trade] = await market.trades()
    assert trade.exit_reason == "manual" and {leg["side"] for leg in trade.legs} == {"short", "long"}


async def test_in_a_backtest_the_last_price_stands_for_bid_and_ask(market, monkeypatch):
    monkeypatch.delattr(NativeContext, "get_bid_ask")
    ctx = await market.check(at(4, 9, 30))
    legs = ctx.state["position"]["legs"]
    assert legs["short_ce"]["entry_price"] == tick_engine.get_current_price(uuid.UUID(legs["short_ce"]["instrument_id"]))
