"""Conti Calendar 15 MIN (native_strategies/nifty_conti_calendar.py, 4 Nov;
version 2, 5 Nov): Conti Straddle's short weekly straddle with the same
strike's monthly CE and PE bought, 16 lots a leg, every fill at bid / ask;
held with no rolls until Rs 14,000 after charges at bid / ask, or 15:29 --
every day, nothing overnight (15:00 until version 8, 8 Nov)."""

import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

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


def gain(market, position: dict, each: float) -> None:
    """Price every leg so that closing it now gains `each` a unit (a loss when
    negative): a sold leg's ask, a bought leg's bid, `each` the right side of its entry."""
    for leg in position["legs"].values():
        option = next(o for o in market.options if str(o.id) == leg["instrument_id"])
        close_at = leg["entry_price"] - each if leg["side"] == "sell" else leg["entry_price"] + each
        market.premium[(option.expiry, option.strike, option.option_type)] = close_at - SPREAD if leg["side"] == "sell" else close_at + SPREAD


async def test_at_1529_one_in_profit_closes_and_one_in_loss_is_carried_overnight(market):
    """12 Nov (version 11): at 15:29, with the take-profit not reached, a
    position in profit ("Net if closed now" Rs 0 or more) closes; one in loss
    is carried -- the take-profit checked every second, each later 15:29
    deciding the same way. Not 15:30 itself; no new entry after 15:00."""
    assert cal.DAILY_CLOSE == dtime(15, 29) and cal.ENTRY_UNTIL == dtime(15, 0)
    p = (await market.check(at(4, 9, 30))).state["position"]
    held = await market.check(at(4, 15, 28, 59))
    assert held.state["position"] is not None and "at 15:29 closed in profit, carried in loss" in held._last_reason
    assert held.state["position"]["net_if_closed_now"] < 0  # the spreads and charges paid, nothing earned yet
    carried = await market.check(at(4, 15, 29))
    assert carried.state["position"] is not None and await market.trades() == []
    assert "in loss at 15:29 -- carried overnight" in carried._last_reason and carried.state["position"]["carried_from"] == "2026-11-04"
    assert carried._wake_at == at(4, 15, 29, 1)

    morning = await market.check(at(5, 9, 15))  # no closing at the first check of the next day
    assert morning.state["position"] is not None and morning._last_action == "hold"
    gain(market, p, 3.0)  # in profit by 15:29, short of Rs 14,000
    closed = await market.check(at(5, 15, 29))
    assert closed.state.get("position") is None and closed._last_reason.startswith("15:29 close in profit (Rs ")
    [trade] = await market.trades()
    assert trade.exit_reason == "time_cutoff_329pm" and len(trade.legs) == 4
    shorts = sorted(leg["exit_price"] for leg in trade.legs if leg["side"] == "short")
    assert shorts == pytest.approx(sorted(p["legs"][n]["entry_price"] - 3.0 for n in ("short_ce", "short_pe")))  # bought back at the ask
    later = await market.check(at(5, 15, 29, 10))
    assert later.state.get("position") is None and "outside entry window" in later._last_reason
    market.premium.clear()
    nxt = await market.check(at(6, 9, 30))
    assert nxt.state["position"] is not None and nxt.state["position"]["weekly_expiry"] == "2026-11-09"


async def test_a_carried_position_on_its_weekly_expiry_day_in_loss_is_rolled_into_a_new_one_at_1500(market):
    """12 Nov (version 11): never held past its weekly's expiry. At 15:00
    that day, in loss: all four close ("expiry_roll") and a new position
    opens at once by the usual rule -- next week's weekly, its monthly, the
    PCR's strike near NIFTY now -- whatever the time, the pause, the 14:00 rule."""
    p = (await market.check(at(4, 9, 30))).state["position"]
    await market.check(at(4, 15, 29))  # in loss: carried
    gain(market, p, -2.0)
    before = await market.check(at(9, 14, 59, 59))
    assert before.state["position"]["weekly_expiry"] == W1.isoformat() and before._last_action == "hold"
    rolled = await market.check(at(9, 15, 0), spot=23560.0)
    [trade] = await market.trades()
    assert trade.exit_reason == "expiry_roll" and len(trade.legs) == 4
    q = rolled.state["position"]
    assert rolled._last_signal == "CALENDAR" and rolled._last_reason.startswith("expiry roll: the 09 Nov weekly expiring with Rs -")
    assert (q["weekly_expiry"], q["monthly_expiry"]) == (W2.isoformat(), W3.isoformat()) and q["opened_at"] == at(9, 15, 0).astimezone(timezone.utc).isoformat()
    assert "reopen_due" not in rolled.state and rolled._wake_at == at(9, 15, 0, 1)


async def test_a_carried_position_on_its_weekly_expiry_day_in_profit_just_closes(market):
    p = (await market.check(at(4, 9, 30))).state["position"]
    await market.check(at(4, 15, 29))  # in loss: carried
    gain(market, p, 3.0)  # by the expiry day it's in profit, short of Rs 14,000
    done = await market.check(at(9, 15, 0))
    [trade] = await market.trades()
    assert trade.exit_reason == "weekly_expiry_3pm" and done.state.get("position") is None
    assert done._last_reason.startswith("the 09 Nov weekly expiring, in profit")
    later = await market.check(at(9, 15, 0, 30))
    assert later.state.get("position") is None and "outside entry window" in later._last_reason  # nothing new that day


async def test_a_roll_missed_while_the_app_was_down_happens_at_the_first_check_after(market):
    await market.check(at(4, 9, 30))
    await market.check(at(4, 15, 29))  # carried
    late = await market.check(at(11, 9, 20))  # down over 9 Nov's 15:00 (10 Nov a holiday): the weekly has expired
    [trade] = await market.trades()
    assert trade.exit_reason == "expiry_roll"  # in loss: rolled at once, before the entry window even
    assert late.state["position"]["weekly_expiry"] == W2.isoformat() and late.state["position"]["monthly_expiry"] == W3.isoformat()


async def test_with_a_leg_unquoted_at_1529_profit_or_loss_cant_be_told_so_it_is_held(market):
    await market.check(at(4, 9, 30))
    market.no_book = {(W1, 23450.0, "CE")}
    held = await market.check(at(4, 15, 29))
    assert held.state["position"] is not None and "profit or loss unknown, held" in held._last_reason


async def test_it_takes_the_profit_once_closing_now_at_bid_and_ask_would_give_14000_after_charges(market):
    assert cal.TAKE_PROFIT == 14_000.0
    held = (await market.check(at(4, 9, 30))).state["position"]

    def decay_to(each: float) -> None:  # the weekly's two premiums fall; the monthly's are as the day prices them
        market.premium[(W1, 23450.0, "CE")] = market.premium[(W1, 23450.0, "PE")] = each

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

    assert "next entry from 11:01:10" in done._last_reason and done._wake_at == at(4, 11, 1, 10)
    paused = await market.check(at(4, 11, 0, 20))  # a minute's pause (version 10; 5 minutes in 7-9)
    assert paused.state.get("position") is None and "next entry from 11:01:10" in paused._last_reason
    again = await market.check(at(4, 11, 1, 10))  # then the next opens
    assert again.state["position"] is not None and again._last_signal == "CALENDAR"


async def test_on_an_expiry_day_it_opens_on_next_weeks_and_in_the_months_last_week_buys_next_months(market):
    expiry_day = await market.check(at(9, 9, 30))
    assert expiry_day.state["position"]["weekly_expiry"] == "2026-11-17"
    assert (await market.check(at(9, 15, 29))).state["position"] is not None  # in loss at 15:29: carried (version 11)
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
    assert ctx.state["position"] is None and ctx._last_reason == "manual exit; next entry from 10:01:00"
    assert ctx._wake_at == at(4, 10, 1)
    [trade] = await market.trades()
    assert trade.exit_reason == "manual" and {leg["side"] for leg in trade.legs} == {"short", "long"}


async def test_in_a_backtest_the_last_price_stands_for_bid_and_ask(market, monkeypatch):
    monkeypatch.setattr(NativeContext, "is_backtest", True)  # BacktestNativeContext's flag: no order book to read
    ctx = await market.check(at(4, 9, 30))
    legs = ctx.state["position"]["legs"]
    assert legs["short_ce"]["entry_price"] == tick_engine.get_current_price(uuid.UUID(legs["short_ce"]["instrument_id"]))


async def test_the_card_shows_the_profit_at_the_close_across_nifty_levels(market):
    """5 Nov: the card's payoff (services/options/calendar_payoff.py) -- built
    from the position and the live book, while the 15:29 close is still ahead."""
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.check(at(4, 9, 30))
    market.prices(at(4, 11, 0), 23480.0)
    out = await ep._build_position_out(market.db, market.deployment.state, at(4, 11, 0).astimezone(timezone.utc))
    payoff = out.payoff
    assert payoff is not None and payoff.horizon == at(4, 15, 29) and payoff.spot == 23480.0
    assert payoff.points[0].spot == 22480.0 and payoff.points[-1].spot == 24480.0
    assert payoff.target == cal.TAKE_PROFIT and set(payoff.implied_vols) == {"short_ce", "short_pe", "long_ce", "long_pe"}
    assert payoff.max_loss <= payoff.max_profit and payoff.loss_cap is not None and payoff.loss_cap > 0

    market.prices(at(4, 15, 0), 23480.0)
    three = await ep._build_position_out(market.db, market.deployment.state, at(4, 15, 0, 5).astimezone(timezone.utc))
    assert three.payoff is not None and three.payoff.horizon == at(4, 15, 29)  # 15:00 isn't the close any more
    market.prices(at(4, 15, 29), 23480.0)
    late = await ep._build_position_out(market.db, market.deployment.state, at(4, 15, 29, 5).astimezone(timezone.utc))
    assert late.payoff is None  # 15:29 has come: it's closing


async def test_the_telegram_entry_alert_carries_the_setup_with_its_close_figures(market):
    """5 Nov: the entry alert (notifications/trade_alerts.py) works out the
    same figures at the 15:29 close as the card, from the position just opened."""
    from app.services.notifications import trade_alerts

    ctx = await market.check(at(4, 9, 30))
    position = ctx.state["position"]
    payoff = await trade_alerts._entry_payoff(market.db, position, ctx, 23460.0)
    assert payoff is not None and payoff.horizon == at(4, 15, 29) and payoff.target == cal.TAKE_PROFIT
    title, body = trade_alerts.entry_message("Conti Calendar 15 MIN", ctx.opened_legs, position, ctx.now, payoff)
    assert title == "📅 Conti Calendar 15 MIN: trade taken" and "Strike 23,450 · 16 lots a leg" in body
    assert "Target +14,000 after costs" in body and "15:29: closed if in profit," in body and "At 15:29, IV as now:" in body and " Max loss " in body
    assert await trade_alerts._entry_payoff(market.db, {"regime": "bullish"}, ctx, 23460.0) is None


def test_the_strikes_looked_at_are_every_50_points():
    """6 Nov (version 6): version 3's hundreds-only rule taken off again -- the
    PCR picks among every 50-point strike, as Conti does."""
    assert cal.candidate_strikes(23460.0) == [23300, 23350, 23400, 23450, 23500, 23550, 23600]
    assert cal.candidate_strikes(24268.0) == [24100, 24150, 24200, 24250, 24300, 24350, 24400]


async def test_there_is_no_limit_on_trades_a_day(market):
    """6 Nov (version 4): version 3's two-a-day limit is gone -- each close
    before 15:00 is followed by the next trade (5 minutes on, version 7)."""
    for n, (opens, closes) in enumerate(((at(4, 9, 30), at(4, 10, 0)), (at(4, 10, 5), at(4, 10, 30)), (at(4, 10, 35), at(4, 11, 0)))):
        ctx = await market.check(opens)
        assert ctx.state["position"] is not None and ctx.state["position"]["strike"] % 50 == 0, n
        assert "trades_today" not in ctx.state
        market.deployment.state = {**market.deployment.state, "force_exit": True}
        await market.check(closes)
    assert len(await market.trades()) == 3
    fourth = await market.check(at(4, 11, 5))
    assert fourth.state["position"] is not None and fourth._last_signal == "CALENDAR"


async def test_after_a_close_it_waits_a_minute_then_opens_the_next_right_away(market):
    """6 Nov (version 7; 1 minute since version 10, 11 Nov): no new position
    within a minute of one closing -- closed 11:27:15, the next from
    11:28:15 -- kept in the state, so a restart doesn't cut it short; past
    15:00 none opens anyway."""
    assert cal.PAUSE_AFTER_CLOSE == timedelta(minutes=1)
    await market.check(at(4, 11, 0))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    await market.check(at(4, 11, 27, 15))
    assert market.deployment.state["paused_until"] == at(4, 11, 28, 15).astimezone(timezone.utc).isoformat()
    for second in (at(4, 11, 27, 16), at(4, 11, 28), at(4, 11, 28, 14)):
        ctx = await market.check(second)
        assert ctx.state.get("position") is None and ctx._last_signal is None, second
        assert ctx._last_reason == "paused 1 minute after the last close: next entry from 11:28:15" and ctx._wake_at == at(4, 11, 28, 15)
    opened = await market.check(at(4, 11, 28, 15))
    assert opened.state["position"] is not None and opened._last_signal == "CALENDAR" and "paused_until" not in opened.state

    market.deployment.state = {**market.deployment.state, "force_exit": True}
    await market.check(at(4, 14, 57))
    late = await market.check(at(4, 15, 2))
    assert late.state.get("position") is None and "outside entry window" in late._last_reason
    assert (await market.check(at(5, 9, 30))).state["position"] is not None  # the next morning as usual


async def test_a_close_from_1400_ends_the_days_new_trades_and_one_before_is_held_to_the_close(market):
    """9 Nov (version 9): once a position closes at or after 14:00 -- by
    take-profit or a manual exit -- none opens again that day; a close
    just before 14:00 still lets the next open after its pause; one opened
    before 14:00 that hasn't hit the take-profit is held to 15:29."""
    assert cal.DAY_DONE_AFTER == dtime(14, 0)
    await market.check(at(4, 13, 0))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    before = await market.check(at(4, 13, 59, 30))  # just before 14:00: the pause, then the next
    assert before._last_reason == "manual exit; next entry from 14:00:30" and before._wake_at == at(4, 14, 0, 30)
    reopened = await market.check(at(4, 14, 0, 30))
    assert reopened.state["position"] is not None and reopened._last_signal == "CALENDAR"

    market.deployment.state = {**market.deployment.state, "force_exit": True}
    done = await market.check(at(4, 14, 12, 5))  # at 14:12: the day's last
    assert done._last_reason == "manual exit; no new trade today (closed after 14:00)" and done._wake_at is None
    for later in (at(4, 14, 17, 5), at(4, 14, 45), at(4, 14, 59, 59)):
        ctx = await market.check(later)
        assert ctx.state.get("position") is None and ctx._last_signal is None, later
        assert ctx._last_reason == "no new trade today: the last closed at 14:12:05, after 14:00; next from 09:30 tomorrow"
    assert (await market.check(at(5, 9, 30))).state["position"] is not None  # the next morning as usual

    held = await market.check(at(5, 14, 30))  # opened before 14:00, short of the take-profit: held
    assert held.state["position"] is not None and "at 15:29 closed in profit, carried in loss" in held._last_reason
    carried = await market.check(at(5, 15, 29))  # in loss at 15:29: carried overnight (version 11)
    assert carried.state["position"] is not None and "carried overnight" in carried._last_reason


async def test_a_take_profit_from_1400_also_ends_the_days_new_trades(market):
    held = (await market.check(at(4, 13, 50))).state["position"]
    for leg in ("short_ce", "short_pe"):  # the weekly's premiums collapse: closing now clears Rs 14,000
        market.premium[(date.fromisoformat(held["weekly_expiry"]), held["strike"], held["legs"][leg]["option_type"])] = 5.0
    tp = await market.check(at(4, 14, 0))
    assert tp.state["position"] is None and "take-profit" in tp._last_reason and tp._wake_at is None
    assert tp._last_reason.endswith("no new trade today (closed after 14:00)")
    assert "no new trade today" in (await market.check(at(4, 14, 5)))._last_reason


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
    market.no_book = {(W1, 23450.0, "CE")}
    waiting = await market.check(at(4, 9, 30, 2))
    assert "no bid/ask for short_ce" in waiting._last_reason and waiting._wake_at == at(4, 9, 30, 3)
    market.no_book = set()
    closed = await market.check(at(4, 15, 29))
    assert closed.state["position"] is not None and closed._wake_at == at(4, 15, 29, 1)  # in loss: carried, still watched


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
        assert body["bias"] == "calendar" and len(body["legs"]) == 4 and body["metrics"]["strike"] == 23450
        stranger = User(email=f"x_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="X")
        market.db.add(stranger)
        await market.db.commit()
        app.dependency_overrides[get_current_user] = lambda: stranger
        assert (await client.get(url)).status_code == 403  # the app's ownership check
    finally:
        app.dependency_overrides.pop(get_current_user, None)
