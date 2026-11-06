"""Conti Calendar 15 MIN (native_strategies/nifty_conti_calendar.py, 4 Nov;
version 2, 5 Nov): Conti Straddle's short weekly straddle with the same
strike's monthly CE and PE bought, 16 lots a leg, every fill at bid / ask;
held with no rolls until Rs 14,000 after charges at bid / ask, or 15:00 --
every day, nothing overnight."""

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
            for e in SYMBOLS for k in range(23000, 24001, 50) for t in ("CE", "PE")
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
    assert (p["strike"], p["side"], p["weekly_expiry"], p["monthly_expiry"]) == (23400, "call", "2026-11-09", "2026-11-23")
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


async def test_if_the_take_profit_hasnt_come_all_four_close_at_1500_every_day_and_the_next_opens_at_0930(market):
    await market.check(at(4, 9, 30))
    held = await market.check(at(4, 14, 59, 50))
    assert held.state["position"] is not None and "closes at 15:00 otherwise" in held._last_reason
    assert held.state["position"]["net_if_closed_now"] < 0  # the spreads and charges paid, nothing earned yet
    closed = await market.check(at(4, 15, 0))
    assert closed.state["position"] is None and closed._last_reason.startswith("15:00 close: all four legs closed at bid/ask")
    [trade] = await market.trades()
    assert trade.exit_reason == "time_cutoff_3pm" and len(trade.legs) == 4
    exits = {leg["side"]: leg for leg in trade.legs}
    short_ltp = tick_engine.get_current_price(uuid.UUID(exits["short"]["instrument_id"]))
    assert exits["short"]["exit_price"] == short_ltp + SPREAD  # bought back at the ask
    later = await market.check(at(4, 15, 10))
    assert later.state.get("position") is None and "outside entry window" in later._last_reason
    nxt = await market.check(at(5, 9, 30))
    assert nxt.state["position"] is not None and nxt.state["position"]["weekly_expiry"] == "2026-11-09"


async def test_one_left_open_from_an_earlier_day_closes_at_its_first_check(market):
    await market.check(at(4, 9, 30))  # and the app is down from before 15:00 until the next morning
    ctx = await market.check(at(5, 9, 15))
    assert ctx.state["position"] is None and "open since 04 Nov, past its 15:00 close" in ctx._last_reason
    [trade] = await market.trades()
    assert trade.exit_reason == "time_cutoff_3pm"


async def test_it_takes_the_profit_once_closing_now_at_bid_and_ask_would_give_14000_after_charges(market):
    assert cal.TAKE_PROFIT == 14_000.0
    held = (await market.check(at(4, 9, 30))).state["position"]

    def decay_to(each: float) -> None:  # the weekly's two premiums fall; the monthly's are as the day prices them
        market.premium[(W1, 23400.0, "CE")] = market.premium[(W1, 23400.0, "PE")] = each

    market.prices(at(4, 11, 0), 23460.0)  # the book later that day
    longs = {n: tick_engine.get_fresh_bid_ask(uuid.UUID(held["legs"][n]["instrument_id"]), timedelta(minutes=1), at(4, 11, 0))
             for n in ("long_ce", "long_pe")}

    def net(each: float) -> float:
        return cal.if_closed_now(held, {"short_ce": (each - SPREAD, each + SPREAD), "short_pe": (each - SPREAD, each + SPREAD), **longs})[0]

    lo, hi = 0.0, 300.0  # the weekly premium where closing now gives exactly Rs 14,000 lies between
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if net(mid) >= cal.TAKE_PROFIT else (lo, mid)

    decay_to(hi + 0.05)  # just short of Rs 14,000: held
    under = await market.check(at(4, 11, 0))
    assert under.state["position"] is not None and under.state["position"]["net_if_closed_now"] == pytest.approx(net(hi + 0.05), abs=0.01)
    assert under.state["position"]["net_if_closed_now"] < cal.TAKE_PROFIT

    decay_to(lo - 0.05)  # just past it: all four close at bid / ask
    done = await market.check(at(4, 11, 0, 10))
    assert done.state["position"] is None and done._last_signal == "COVER" and "take-profit" in done._last_reason
    [trade] = await market.trades()
    assert trade.exit_reason == "take_profit"
    exits = {(leg["side"], leg["exit_price"]) for leg in trade.legs}
    assert ("short", lo - 0.05 + SPREAD) in exits  # bought back at the ask
    assert ("long", longs["long_ce"][0]) in exits and ("long", longs["long_pe"][0]) in exits  # sold at the bid

    again = await market.check(at(4, 11, 0, 20))  # the next check opens the next one
    assert again.state["position"] is not None and again._last_signal == "CALENDAR"


async def test_on_an_expiry_day_it_opens_on_next_weeks_and_in_the_months_last_week_buys_next_months(market):
    expiry_day = await market.check(at(9, 9, 30))
    assert expiry_day.state["position"]["weekly_expiry"] == "2026-11-17"
    assert (await market.check(at(9, 15, 0))).state["position"] is None  # closed at 15:00 like any day
    market.deployment.state = None
    market.weeklies = [W3, DEC]
    last_week = await market.check(at(18, 9, 30))
    p = last_week.state["position"]
    assert (p["weekly_expiry"], p["monthly_expiry"]) == ("2026-11-23", "2026-12-29")
    assert p["legs"]["long_ce"]["expiry"] == "2026-12-29"


async def test_no_bid_or_ask_on_a_leg_means_no_entry_and_no_take_profit_reading(market):
    market.no_book = {(W3, 23400.0, "PE")}
    ctx = await market.check(at(4, 9, 30))
    assert ctx.state.get("position") is None and "no bid/ask for the 23,400 PE monthly" in ctx._last_reason
    market.no_book = set()
    await market.check(at(4, 9, 31))
    market.no_book = {(W1, 23400.0, "CE")}
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
    monkeypatch.setattr(NativeContext, "is_backtest", True)  # BacktestNativeContext's flag: no order book to read
    ctx = await market.check(at(4, 9, 30))
    legs = ctx.state["position"]["legs"]
    assert legs["short_ce"]["entry_price"] == tick_engine.get_current_price(uuid.UUID(legs["short_ce"]["instrument_id"]))


async def test_the_card_shows_the_profit_at_1500_across_nifty_levels(market):
    """5 Nov: the card's payoff (services/options/calendar_payoff.py) -- built
    from the position and the live book, while 15:00 is still ahead."""
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.check(at(4, 9, 30))
    market.prices(at(4, 11, 0), 23480.0)
    out = await ep._build_position_out(market.db, market.deployment.state, at(4, 11, 0).astimezone(timezone.utc))
    payoff = out.payoff
    assert payoff is not None and payoff.horizon == at(4, 15, 0) and payoff.spot == 23480.0
    assert payoff.points[0].spot == 22480.0 and payoff.points[-1].spot == 24480.0
    assert payoff.target == cal.TAKE_PROFIT and set(payoff.implied_vols) == {"short_ce", "short_pe", "long_ce", "long_pe"}
    assert payoff.max_loss <= payoff.max_profit and payoff.loss_cap is not None and payoff.loss_cap > 0

    market.prices(at(4, 15, 0), 23480.0)
    late = await ep._build_position_out(market.db, market.deployment.state, at(4, 15, 0, 5).astimezone(timezone.utc))
    assert late.payoff is None  # 15:00 has come: it's closing


async def test_the_telegram_entry_alert_carries_the_setup_with_its_1500_figures(market):
    """5 Nov: the entry alert (notifications/trade_alerts.py) works out the
    same 15:00 figures as the card, from the position just opened."""
    from app.services.notifications import trade_alerts

    ctx = await market.check(at(4, 9, 30))
    position = ctx.state["position"]
    payoff = await trade_alerts._entry_payoff(market.db, position, ctx, 23460.0)
    assert payoff is not None and payoff.horizon == at(4, 15, 0) and payoff.target == cal.TAKE_PROFIT
    title, body = trade_alerts.entry_message("Conti Calendar 15 MIN", ctx.opened_legs, position, ctx.now, payoff)
    assert title == "📅 Conti Calendar 15 MIN: trade taken" and "Strike 23,400 · 16 lots a leg" in body
    assert "Target +14,000 after costs" in body and "At 15:00, IV as now:" in body and " Max loss " in body
    assert await trade_alerts._entry_payoff(market.db, {"regime": "bullish"}, ctx, 23460.0) is None


def test_only_strikes_in_hundreds_are_looked_at():
    """6 Nov (version 3): the PCR picks among 24,200, 24,300 ... -- never 24,250."""
    assert cal.candidate_strikes(23460.0) == [23200, 23300, 23400, 23500, 23600, 23700, 23800]
    assert cal.candidate_strikes(24268.0) == [24000, 24100, 24200, 24300, 24400, 24500, 24600]


async def test_there_is_no_limit_on_trades_a_day(market):
    """6 Nov (version 4): version 3's two-a-day limit is gone -- each close
    before 15:00 is followed by the next trade."""
    for n, (opens, closes) in enumerate(((at(4, 9, 30), at(4, 10, 0)), (at(4, 10, 1), at(4, 10, 30)), (at(4, 10, 31), at(4, 11, 0)))):
        ctx = await market.check(opens)
        assert ctx.state["position"] is not None and ctx.state["position"]["strike"] % 100 == 0, n
        assert "trades_today" not in ctx.state
        market.deployment.state = {**market.deployment.state, "force_exit": True}
        await market.check(closes)
    assert len(await market.trades()) == 3
    fourth = await market.check(at(4, 11, 1))
    assert fourth.state["position"] is not None and fourth._last_signal == "CALENDAR"


async def test_while_a_position_is_open_it_asks_to_be_checked_again_a_second_on(market):
    """6 Nov (version 5): "Net if closed now" and the take-profit are read
    every second while a position is open, and the next position opens a
    second after a take-profit; flat and outside its window, no hurry."""
    from datetime import timedelta as td

    early = await market.check(at(4, 9, 29))
    assert early._wake_at is None
    opened = await market.check(at(4, 9, 30))
    assert opened._wake_at == at(4, 9, 30) + td(seconds=1)
    held = await market.check(at(4, 9, 30, 1))
    assert held.state["position"] is not None and held._wake_at == at(4, 9, 30, 2)
    market.no_book = {(W1, 23400.0, "CE")}
    waiting = await market.check(at(4, 9, 30, 2))
    assert "no bid/ask for short_ce" in waiting._last_reason and waiting._wake_at == at(4, 9, 30, 3)
    market.no_book = set()
    closed = await market.check(at(4, 15, 0))
    assert closed.state["position"] is None and closed._wake_at is None  # the day's close: nothing until tomorrow


async def test_the_card_can_fetch_its_position_alone(market, client, monkeypatch):
    """6 Nov: GET /native-deployments/{id}/position -- what a calendar card
    re-reads every second; null when flat, 403 for someone else's card."""
    from app.api.v1.endpoints import paper_native_trading as ep
    from app.core.deps import get_current_user
    from app.main import app
    from app.models.user import User

    owner = await market.db.get(User, market.portfolio.user_id)
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        url = f"/api/v1/paper-trading/native-deployments/{market.deployment.id}/position"
        assert (await client.get(url)).json() is None  # flat
        await market.check(at(4, 9, 30))

        original = ep._build_position_out

        async def built(db, state, now=None):  # as at 09:31, when the market is open
            return await original(db, state, at(4, 9, 31))

        monkeypatch.setattr(ep, "_build_position_out", built)
        body = (await client.get(url)).json()
        assert body["bias"] == "calendar" and len(body["legs"]) == 4 and body["metrics"]["strike"] == 23400
        stranger = User(email=f"x_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="X")
        market.db.add(stranger)
        await market.db.commit()
        app.dependency_overrides[get_current_user] = lambda: stranger
        assert (await client.get(url)).status_code == 403  # the app's ownership check
    finally:
        app.dependency_overrides.pop(get_current_user, None)
