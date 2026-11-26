"""Conti Calendar 400 15 MIN (native_strategies/nifty_conti_calendar_400.py,
11 Nov): Conti Calendar 15 MIN copied as a double calendar -- the weekly CE
sold and the monthly CE bought at an OTM call strike, the weekly PE sold
and the monthly PE bought at an OTM put strike (since version 3, 12 Nov:
on each side the furthest OTM 50-point strike whose weekly bid is still
Rs 100 or more; ATM+-400 before; since version 4, 16 Nov: the next expiry
when the nearest has no such strike on a side), 20 lots a leg at bid / ask (16 before version 6, 20 Nov,
which also shifts the side NIFTY moves toward: 150 points in loss, 200 in profit) -- held overnight, the sold weeklies rolled at 15:00 on their expiry
day (all four reopened on next month's monthly in the month's last week),
closed at Rs 14,000 after charges, every trade keeping the most profit and
loss it reached. Since version 9 (26 Nov) the bought hedge is the weekly two
weeks after the sold one at the same strike (a monthly before), the sold
weeklies roll at 15:00 on the trading day before their expiry -- every
second week all four close and reopen ("hedge_roll") -- and a strike shift
moves the side's hedge with it (a shift at the profit chart's peak, also
considered, was left out after the backtest)."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_conti_calendar_400 as c400
from app.models.instrument import Instrument
from app.models.paper_trading import PaperNativeTrade
from app.services.market_data.tick_engine import tick_engine
from app.services.notifications.trade_alerts import entry_message, exit_message
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_calendar import SPREAD, W1, W2, W3, Market, at

WIDTH = 32  # the Telegram group's message block


def premium(spot: float, strike: float, option_type: str, weeks: float) -> float:
    intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    return max(5.0, 220.0 - 0.4 * abs(spot - strike)) * weeks ** 0.5 + intrinsic


CE, PE = 23750.0, 23200.0  # NIFTY 23,460's strikes in this market
D1, D8 = date(2026, 12, 1), date(2026, 12, 8)  # version 9: weeklies after November's -- hedges two weeks out


class Market400(Market):
    """Conti Calendar's test market, run with the double calendar; an
    option's time value falls 0.40 a point from 220 at the money (to 5 at
    least), times the square root of its weeks to expiry, plus what's in the
    money. NIFTY 23,460: the 23,750 CE's weekly bid 103.5 (the 23,800's
    83.5), the 23,200 PE's 115.5 (the 23,150's 95.5). Its weeklies run on
    to 1 and 8 Dec (version 9: the hedges two weeks out)."""

    async def build(self, monkeypatch) -> "Market400":
        await super().build(monkeypatch)
        extra = [
            Instrument(exchange="NFO", symbol=f"{sym}{k}{t}", name="opt", instrument_type="option", data_source="zerodha_kite",
                       external_ref=f"{sym}{k}{t}", expiry=e, strike=float(k), option_type=t, lot_size=65,
                       underlying_instrument_id=self.nifty.id)
            for e, sym in ((D1, "NIFTY26D01"), (D8, "NIFTY26D08")) for k in range(23000, 24001, 50) for t in ("CE", "PE")
        ]
        self.db.add_all(extra)
        await self.db.commit()
        self.options += extra
        self.weeklies = [W1, W2, W3, D1, D8]
        return self

    def prices(self, now: datetime, spot: float) -> None:
        tick_engine.set_real_price(self.nifty.id, spot, source="test")
        tick_engine._real_price_at[self.nifty.id] = now
        for o in self.options:
            weeks = max(1, (o.expiry - now.date()).days / 7)
            intrinsic = max(0.0, spot - o.strike) if o.option_type == "CE" else max(0.0, o.strike - spot)
            ltp = self.premium.get((o.expiry, o.strike, o.option_type), premium(spot, o.strike, o.option_type, weeks))
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
        await c400.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Market400(db_session).build(monkeypatch)
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def _legs(position: dict) -> dict[str, tuple]:
    return {name: (leg["side"], leg["option_type"], leg["strike"], leg["expiry"]) for name, leg in position["legs"].items()}


def test_the_first_otm_strike_each_side_is_the_nearest_50_point_one_beyond_nifty():
    assert (c400.first_otm(25310.0, "CE"), c400.first_otm(25310.0, "PE")) == (25350.0, 25300.0)
    assert (c400.first_otm(25300.0, "CE"), c400.first_otm(25300.0, "PE")) == (25350.0, 25250.0)  # at the money: neither
    assert c400.MIN_PREMIUM == 100.0 and c400.STRIKE_STEP == 50


async def test_each_side_takes_the_furthest_otm_strike_whose_weekly_bid_is_still_100(market):
    """12 Nov (version 3): however far from NIFTY -- walked out until a bid falls under Rs 100."""
    p = (await market.check(at(4, 9, 30))).state["position"]
    assert (p["strike_ce"], p["strike_pe"]) == (CE, PE) and p["min_premium"] == 100.0
    assert p["legs"]["short_ce"]["entry_price"] == pytest.approx(103.5) and p["legs"]["short_pe"]["entry_price"] == pytest.approx(115.5)
    market.deployment.state = None

    moved = await market.check(at(5, 9, 30), spot=23360.0)  # NIFTY elsewhere: so are the strikes, wherever Rs 100 is
    assert (moved.state["position"]["strike_ce"], moved.state["position"]["strike_pe"]) == (23650.0, 23100.0)
    market.deployment.state = None
    edge = await market.check(at(5, 9, 31), spot=23060.0)  # the listed puts end at 23,000 still over Rs 100: the last listed
    assert (edge.state["position"]["strike_ce"], edge.state["position"]["strike_pe"]) == (23350.0, 23000.0)
    market.deployment.state = None

    market.premium = {(e, k, "CE"): 99.0 for e in (W2, W3) for k in range(23000, 24001, 50)}  # no call at Rs 100 on 17 or 23 Nov
    none = await market.check(at(9, 9, 30))
    assert none.state.get("position") is None and none._last_reason == (
        "no OTM 17 Nov CE with a bid of Rs 100 or more; nor the next: no OTM 23 Nov CE with a bid of Rs 100 or more")

    market.premium = {}
    market.no_book = {(W2, 23300.0, "PE")}  # an OTM put on the way out with no book: no entry this check
    waiting = await market.check(at(9, 9, 31))
    assert waiting.state.get("position") is None and waiting._last_reason == "no bid/ask for the 17 Nov 23,300 PE yet"
    market.no_book = set()
    assert (await market.check(at(9, 9, 32))).state["position"] is not None  # the next check: in


async def test_with_no_rs_100_strike_on_the_nearest_weekly_it_sells_the_next_expiry(market):
    """16 Nov (version 4): 13 Nov, the 17 Nov calls with no OTM bid of Rs 100
    -- both sides then sold on the expiry after it (23 Nov) at its Rs 100
    strikes, the hedges two weeks after that (8 Dec, version 9) at the same strikes."""
    market.premium = {(W2, k, "CE"): 60.0 for k in range(23000, 24001, 50)}  # the 17 Nov calls cheap
    ctx = await market.check(at(13, 9, 30))
    p = ctx.state["position"]
    assert (p["weekly_expiry"], p["hedge_expiry"]) == (W3.isoformat(), D8.isoformat())
    assert (p["strike_ce"], p["strike_pe"]) == (CE, 23150.0)  # the 23 Nov's Rs 100 strikes, 10 days out
    assert _legs(p) == {
        "short_ce": ("sell", "CE", CE, W3.isoformat()), "long_ce": ("buy", "CE", CE, D8.isoformat()),
        "short_pe": ("sell", "PE", 23150.0, W3.isoformat()), "long_pe": ("buy", "PE", 23150.0, D8.isoformat()),
    }
    assert ctx._last_signal == "CALENDAR"
    assert ("sell 23 Nov at the bid (the 17 Nov weekly had no OTM CE with a bid of Rs 100 or more: the next expiry instead), "
            "buy 08 Dec at the same strikes at the ask") in ctx._last_reason
    market.deployment.state = None

    market.premium = {(W2, k, "PE"): 60.0 for k in range(23000, 24001, 50)}  # the puts short instead: the same
    puts = await market.check(at(13, 9, 31))
    assert puts.state["position"]["weekly_expiry"] == W3.isoformat() and "no OTM PE with a bid" in puts._last_reason
    market.deployment.state = None

    market.premium = {}
    near = await market.check(at(13, 9, 32))  # Rs 100 on the nearest: it is sold, the hedge 1 Dec
    assert (near.state["position"]["weekly_expiry"], near.state["position"]["hedge_expiry"]) == (W2.isoformat(), D1.isoformat())
    assert "the next expiry instead" not in near._last_reason
    market.deployment.state = None

    market.premium = {(W2, k, "CE"): 60.0 for k in range(23000, 24001, 50)}
    market.no_book = {(W3, 23700.0, "CE")}  # the next has a strike on the way with no book: no entry this check
    waiting = await market.check(at(13, 9, 33))
    assert waiting.state.get("position") is None and waiting._last_reason == (
        "no OTM 17 Nov CE with a bid of Rs 100 or more; nor the next: no bid/ask for the 23 Nov 23,700 CE yet")


async def test_a_strike_with_no_book_on_the_nearest_weekly_doesnt_move_it_on(market):
    market.no_book = {(W2, 23300.0, "PE")}
    waiting = await market.check(at(13, 9, 30))
    assert waiting.state.get("position") is None and waiting._last_reason == "no bid/ask for the 17 Nov 23,300 PE yet"


def test_the_hedge_is_the_listed_expiry_two_weeks_after_the_sold_one_and_the_roll_the_trading_day_before_expiry():
    """26 Nov (version 9)."""
    tuesdays = [date(2026, 12, 1), date(2026, 12, 8), date(2026, 12, 15), date(2026, 12, 22), date(2026, 12, 29)]
    assert c400.hedge_after(tuesdays, date(2026, 12, 1)) == date(2026, 12, 15) and c400.HEDGE_DAYS == (11, 17)
    assert c400.hedge_after([W1, W2, W3, D1], W1) == W3  # 9 Nov (a Monday, 10 Nov a holiday) -> 23 Nov
    assert c400.hedge_after([W1, W2, W3, D1], W2) == D1  # 17 Nov -> 1 Dec
    assert c400.hedge_after([W2, W3, D8], W2) is None  # 1 Dec not listed: nothing two weeks on
    assert c400.hedge_after([date(2026, 12, 1), date(2026, 12, 14)], date(2026, 12, 1)) == date(2026, 12, 14)  # a holiday's Monday
    assert (c400.roll_day(W1), c400.roll_day(W2), c400.roll_day(W3)) == (date(2026, 11, 6), date(2026, 11, 16), date(2026, 11, 20))
    assert c400.roll_day(date(2026, 12, 1)) == date(2026, 11, 30) and c400.roll_moment(W2) == at(16, 15, 0)


async def test_with_no_expiry_listed_two_weeks_out_there_is_no_entry(market):
    market.weeklies = [W1, W2]
    ctx = await market.check(at(4, 9, 30))
    assert ctx.state.get("position") is None and ctx._last_reason == "no NIFTY expiry listed two weeks after 09 Nov yet to buy"


async def test_on_a_roll_day_a_new_position_sells_the_weekly_after(market):
    """6 Nov, the 9 Nov weeklies' roll day: the 17 Nov sold, the 1 Dec bought."""
    p = (await market.check(at(6, 10, 0))).state["position"]
    assert (p["weekly_expiry"], p["hedge_expiry"]) == (W2.isoformat(), D1.isoformat())


async def test_it_opens_a_double_calendar_at_0930_at_bid_and_ask(market):
    early = await market.check(at(4, 9, 29))
    assert early.state.get("position") is None and "outside entry window" in early._last_reason
    ctx = await market.check(at(4, 9, 30))
    p = ctx.state["position"]
    assert p["regime"] == "double calendar" and (p["strike_ce"], p["strike_pe"]) == (CE, PE)
    assert _legs(p) == {  # version 9: the hedge the weekly two weeks out, at the same strike
        "short_ce": ("sell", "CE", CE, W1.isoformat()), "long_ce": ("buy", "CE", CE, W3.isoformat()),
        "short_pe": ("sell", "PE", PE, W1.isoformat()), "long_pe": ("buy", "PE", PE, W3.isoformat()),
    }
    hedge = premium(23460.0, CE, "CE", (W3 - date(2026, 11, 4)).days / 7)
    assert p["legs"]["short_ce"]["entry_price"] == pytest.approx(premium(23460.0, CE, "CE", 1) - SPREAD)  # sold at the bid
    assert p["legs"]["long_ce"]["entry_price"] == pytest.approx(hedge + SPREAD)  # bought at the ask
    assert (p["weekly_expiry"], p["hedge_expiry"], p["hedge_rule"]) == (W1.isoformat(), W3.isoformat(), c400.HEDGE_RULE)
    assert "monthly_expiry" not in p and "buy 23 Nov at the same strikes at the ask" in ctx._last_reason
    assert "weeklies roll 06 Nov 15:00" in ctx._last_reason
    assert all(leg["quantity"] == 20 * 65 for leg in p["legs"].values())  # version 6: 20 lots a leg
    assert p["take_profit"] == 14_000.0 and p["entry_charges"] > 0 and ctx._last_signal == "CALENDAR"
    assert ctx._wake_at == at(4, 9, 30) + timedelta(seconds=1)


async def test_it_is_held_overnight_with_no_daily_close(market):
    await market.check(at(4, 9, 30))
    for when in (at(4, 15, 0), at(4, 15, 29), at(5, 9, 15), at(5, 14, 0)):
        ctx = await market.check(when)
        assert ctx.state["position"] is not None and ctx._last_action == "hold", when
    assert await market.trades() == []


async def test_it_takes_the_profit_at_14000_then_pauses_5_minutes_and_keeps_the_extremes(market):
    await market.check(at(4, 9, 30))
    p = dict((await market.check(at(4, 10, 0))).state["position"])  # a copy: the next checks change the position in place
    assert p["max_profit_reached"] < 0 and p["max_loss_reached"] < 0  # the spreads and charges paid
    market.premium[(W1, PE, "PE")] = 180.0  # NIFTY dips toward the put: a loss for now
    dip = await market.check(at(4, 11, 0))
    low = dip.state["position"]["max_loss_reached"]
    assert low < p["max_loss_reached"] and dip.state["position"]["max_loss_at"] == "04 Nov 11:00:00 IST"
    market.premium[(W1, PE, "PE")] = market.premium[(W1, CE, "CE")] = 1.0  # both sold weeklies collapse
    done = await market.check(at(4, 13, 0))
    assert done.state.get("position") is None and done._last_signal == "COVER" and "take-profit" in done._last_reason
    assert "max loss reached" in done._last_reason and done._wake_at == at(4, 13, 1)  # a minute's pause (version 2)
    [trade] = await market.trades()
    assert trade.exit_reason == "take_profit" and len(trade.legs) == 4
    e = trade.extremes
    assert e["max_profit_reached"] >= 14_000 and e["max_profit_at"] == "04 Nov 13:00:00 IST"
    assert e["max_loss_reached"] == low and e["max_loss_at"] == "04 Nov 11:00:00 IST"

    paused = await market.check(at(4, 13, 0, 59))
    assert paused.state.get("position") is None and "next entry from 13:01:00" in paused._last_reason
    market.premium.clear()
    again = await market.check(at(4, 13, 1))
    assert again.state["position"] is not None and "max_profit_reached" not in again.state["position"]


async def test_the_sold_weeklies_roll_at_1500_the_trading_day_before_their_expiry_at_the_same_strikes(market):
    """Version 9: 6 Nov 15:00, the trading day before the 9 Nov expiry; the 23 Nov hedges stay."""
    await market.check(at(4, 9, 30))
    before = await market.check(at(6, 14, 59, 59))
    assert before.state["position"]["weekly_expiry"] == W1.isoformat() and "weeklies roll 06 Nov 15:00" in before._last_reason
    rolled = await market.check(at(6, 15, 0), spot=23700.0)  # NIFTY has moved: the strikes don't
    p = rolled.state["position"]
    assert rolled._last_signal == "ROLL" and "weekly roll 09 Nov -> 17 Nov at the same strikes, the 23 Nov hedges kept" in rolled._last_reason
    assert "next roll 16 Nov 15:00" in rolled._last_reason
    assert _legs(p) == {  # the hedges stay where they were bought
        "short_ce": ("sell", "CE", CE, W2.isoformat()), "long_ce": ("buy", "CE", CE, W3.isoformat()),
        "short_pe": ("sell", "PE", PE, W2.isoformat()), "long_pe": ("buy", "PE", PE, W3.isoformat()),
    }
    assert p["rolls"] == 1 and p["weekly_expiry"] == W2.isoformat() and p["hedge_expiry"] == W3.isoformat()
    assert len(p["closed_legs"]) == 2 and {leg["side"] for leg in p["closed_legs"]} == {"short"}
    assert p["booked"]["charges"] > 0 and p["booked_net"] == pytest.approx(p["booked"]["gross"] - p["booked"]["charges"], abs=0.01)
    assert [leg["side"] for leg in rolled.opened_legs] == ["sell", "sell"]
    assert await market.trades() == []  # a roll isn't a closed trade: the position carries on
    held = await market.check(at(11, 10, 0), spot=23590.0)  # 130 points up: inside both shift levels
    assert held._last_action == "hold" and held.state["position"]["rolls"] == 1


async def test_a_roll_missed_while_the_app_was_down_happens_at_the_first_check_after(market):
    await market.check(at(4, 9, 30))
    late = await market.check(at(11, 9, 30))  # nothing ran on 6 Nov afternoon or 9 Nov
    assert late._last_signal == "ROLL" and late.state["position"]["weekly_expiry"] == W2.isoformat()


async def test_every_second_week_all_four_close_and_reopen_two_weeks_out(market):
    """Version 9: at the 17 Nov weeklies' roll (16 Nov 15:00) the next sold weekly would be the 23 Nov hedges' own:
    all four close ("hedge_roll") and a fresh double calendar opens at once -- sell 23 Nov, buy 8 Dec."""
    await market.check(at(4, 9, 30))
    await market.check(at(6, 15, 0))  # the first roll, to 17 Nov
    reopened = await market.check(at(16, 15, 0), spot=23560.0)
    [trade] = await market.trades()
    assert trade.exit_reason == "hedge_roll" and len(trade.legs) == 6  # the two rolled weeklies and the four closed now
    assert trade.extremes is not None
    p = reopened.state["position"]
    assert reopened._last_signal == "CALENDAR"
    assert reopened._last_reason.startswith("hedge roll: the next weekly, 23 Nov, is the bought hedges' own expiry -- all four closed")
    assert (p["strike_ce"], p["strike_pe"]) == (23850.0, 23300.0)  # NIFTY 23,560's Rs 100 strikes
    assert (p["weekly_expiry"], p["hedge_expiry"]) == (W3.isoformat(), D8.isoformat()) and p["rolls"] == 0
    assert p["legs"]["long_ce"]["strike"] == 23850.0 and p["legs"]["long_pe"]["strike"] == 23300.0
    assert "reopen_due" not in reopened.state and "paused_until" not in reopened.state


async def test_a_reopen_that_cant_be_had_is_tried_again_whatever_the_time(market):
    await market.check(at(4, 9, 30))
    await market.check(at(6, 15, 0))
    market.no_book = {(D8, 23850.0, "CE")}
    waiting = await market.check(at(16, 15, 0), spot=23560.0)
    assert waiting.state.get("position") is None and waiting.state.get("reopen_due") and "no bid/ask" in waiting._last_reason
    assert waiting._last_reason == "no bid/ask for the 08 Dec 23,850 CE yet"
    market.no_book = set()
    later = await market.check(at(16, 15, 10), spot=23560.0)  # past 15:00 and inside the pause: still reopened
    assert later.state["position"] is not None and "reopen_due" not in later.state


async def test_a_manual_exit_closes_every_leg_and_keeps_the_extremes(market):
    await market.check(at(4, 9, 30))
    await market.check(at(4, 9, 31))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    ctx = await market.check(at(4, 10, 0))
    assert ctx.state.get("position") is None and ctx._last_reason.startswith("manual exit")
    [trade] = await market.trades()
    assert trade.exit_reason == "manual" and trade.extremes["max_loss_at"] == "04 Nov 09:31:00 IST"


async def test_the_telegram_alerts_show_the_setup_the_roll_and_the_extremes(market):
    opened = await market.check(at(4, 9, 30))
    title, body = entry_message("Conti Calendar 400 15 MIN", opened.opened_legs, opened.state["position"], opened.now)
    lines = body.split("\n")
    assert title == "📅 Conti Calendar 400 15 MIN: trade taken"
    assert lines[0] == "Double Calendar" and "23,750 CE / 23,200 PE" in lines and "20 lots a leg" in lines
    assert "Target +14,000 after costs" in lines and "Held overnight; weeklies" in lines
    assert " roll 15:00 a day before expiry" in lines
    assert max(len(line) for line in lines) <= WIDTH

    rolled = await market.check(at(6, 15, 0))
    title, body = entry_message("Conti Calendar 400 15 MIN", rolled.opened_legs, rolled.state["position"], rolled.now)
    lines = body.split("\n")
    assert title == "📅 Conti Calendar 400 15 MIN: weekly rolled" and "Weeklies rolled to 17 Nov" in lines
    assert any(line.startswith("Booked so far ") for line in lines) and max(len(line) for line in lines) <= WIDTH

    market.deployment.state = {**market.deployment.state, "force_exit": True}
    closed = await market.check(at(11, 10, 0))
    [trade] = closed.closed_trades
    title, body = exit_message("Conti Calendar 400 15 MIN", trade, -1000.0, 1)
    lines = body.split("\n")
    assert lines[1] == "Held 04Nov 09:30 → 11Nov 10:00" and any(line.startswith("Max profit ") for line in lines)
    assert any(line.startswith("Max loss ") for line in lines) and max(len(line) for line in lines) <= WIDTH


async def test_the_trades_endpoint_shows_the_extremes(market):
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.check(at(4, 9, 30))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    await market.check(at(4, 10, 0))
    rows = [(t, "Conti Calendar 400 15 MIN", "INR") for t in (await market.db.execute(select(PaperNativeTrade))).scalars().all()]
    [out] = await ep._trade_outs(market.db, rows)
    assert out.extremes is not None and set(out.extremes) == {"max_profit_reached", "max_profit_at", "max_loss_reached", "max_loss_at"}
    assert uuid.UUID(out.id)


async def test_the_card_shows_the_payoff_at_the_weekly_roll_with_what_rolls_booked(market):
    """11 Nov: the payoff panel (services/options/calendar_payoff.py) for the
    double calendar is at the sold weeklies' roll -- 15:00 on the trading day
    before their expiry since version 9 -- every point carrying what earlier
    rolls booked."""
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.check(at(4, 9, 30))
    market.prices(at(4, 11, 0), 23460.0)
    first = (await ep._build_position_out(market.db, market.deployment.state, at(4, 11, 0).astimezone(timezone.utc))).payoff
    assert first is not None and first.horizon == at(6, 15, 0) and first.booked == 0.0 and first.target == 14_000.0
    assert first.points[0].spot == 22460.0 and first.points[-1].spot == 24460.0
    inside = max(p.pnl for p in first.points if PE <= p.spot <= CE)
    outside = max(p.pnl for p in first.points if p.spot < 22800.0 or p.spot > 24200.0)
    assert inside > outside  # the tent: best between the strikes

    await market.check(at(6, 15, 0))  # the weeklies roll
    state = market.deployment.state
    booked = state["position"]["booked_net"]
    market.prices(at(11, 10, 0), 23460.0)
    after = (await ep._build_position_out(market.db, state, at(11, 10, 0).astimezone(timezone.utc))).payoff
    assert after.horizon == at(16, 15, 0) and after.booked == pytest.approx(booked)
    without = dict(state, position={**state["position"], "booked": {"gross": 0.0, "charges": 0.0}})
    bare = (await ep._build_position_out(market.db, without, at(11, 10, 0).astimezone(timezone.utc))).payoff
    assert [p.pnl for p in after.points] == pytest.approx([p.pnl + booked for p in bare.points], abs=0.02)


async def test_the_card_values_a_calendars_legs_at_bid_and_ask(market):
    """11 Nov: a calendar's card values each leg at what closing it fills at
    -- a sold leg at the ask, a bought one at the bid -- the last price
    alongside; Unrealized / Total P&L follow. Other positions, and a shut
    market, keep the last price."""
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.check(at(4, 9, 30))
    market.prices(at(4, 11, 0), 23460.0)
    out = await ep._build_position_out(market.db, market.deployment.state, at(4, 11, 0).astimezone(timezone.utc))
    for leg in out.legs:
        assert leg.bid == leg.last_price - SPREAD and leg.ask == leg.last_price + SPREAD
        assert leg.current_price == (leg.ask if leg.side == "short" else leg.bid)
    spread_cost = sum(SPREAD * leg.quantity for leg in out.legs)
    at_last = sum((leg.entry_price - leg.last_price if leg.side == "short" else leg.last_price - leg.entry_price) * leg.quantity
                  for leg in out.legs)
    assert out.unrealized_pnl == pytest.approx(at_last - spread_cost)  # the half-spread on every leg

    other = {**market.deployment.state, "position": {**market.deployment.state["position"], "regime": "sideways"}}
    plain = await ep._build_position_out(market.db, other, at(4, 11, 0).astimezone(timezone.utc))
    assert all(leg.bid is None and leg.current_price == leg_out.last_price for leg, leg_out in zip(plain.legs, out.legs))

    shut = await ep._build_position_out(market.db, market.deployment.state, at(4, 16, 0).astimezone(timezone.utc))
    assert all(leg.bid is None and leg.last_price is None for leg in shut.legs)


async def test_a_position_with_a_monthly_hedge_from_before_is_closed_and_reopened_by_the_new_rule(market):
    """26 Nov (version 9): the open version 8 position -- monthly hedges -- closes at the first check in the entry window."""
    old = (await market.check(at(4, 9, 30))).state["position"]
    old.pop("hedge_rule")
    old["monthly_expiry"] = old.pop("hedge_expiry")
    old["monthly_rule"] = "ask 2.5-3x the weekly's bid"
    market.deployment.state = {**market.deployment.state, "position": old}
    night = await market.check(at(4, 16, 0))  # outside the entry window: left as it is
    assert night._last_action == "hold" and night.state["position"].get("hedge_rule") is None and await market.trades() == []
    early = await market.check(at(5, 9, 20))  # before 09:30: still held, read off its monthly's expiry
    assert early._last_action == "hold" and "(sold 09 Nov, bought 23 Nov)" in early._last_reason and await market.trades() == []
    ctx = await market.check(at(5, 9, 45))
    [trade] = await market.trades()
    p = ctx.state["position"]
    assert trade.exit_reason == "rule_change" and len(trade.legs) == 4 and p["hedge_rule"] == c400.HEDGE_RULE
    assert ctx._last_signal == "CALENDAR" and ctx._last_reason.startswith("the hedge rule changed (version 9")
    assert (p["weekly_expiry"], p["hedge_expiry"]) == (W1.isoformat(), W3.isoformat()) and "reopen_due" not in ctx.state


async def test_only_the_150_200_point_rule_shifts_a_side_not_nifty_at_its_sold_strike(market, monkeypatch):
    """26 Nov: version 9 without the chart-peak shift (left out after the backtest) -- NIFTY at or past a sold
    strike, short of the point rule, shifts nothing."""
    assert not hasattr(c400, "chart_peaks")
    monkeypatch.setattr(c400, "SHIFT_IN_LOSS", 1000.0)
    monkeypatch.setattr(c400, "SHIFT_IN_PROFIT", 1000.0)
    await market.check(at(4, 9, 30))
    for when, spot in ((at(4, 10, 0), CE), (at(4, 10, 1), CE + 20), (at(4, 10, 2), PE), (at(4, 10, 3), PE - 20)):
        held = await market.check(when, spot=spot)
        assert held._last_action == "hold" and held.state["position"].get("shifts") is None, spot
        assert "peak" not in held._last_reason and "shifts the call side over NIFTY" in held._last_reason
    assert await market.trades() == []


async def test_in_loss_the_side_nifty_moves_toward_is_shifted_past_150_points(market):
    """20 Nov (version 6): the side NIFTY moves toward, 150 points past where it was opened, in loss: that side's
    weekly and monthly closed at bid / ask, booked, and opened again at NIFTY's Rs 100 strike now with its 2.5-3x
    monthly (version 9: its hedge at the same strike), the same expiries; the other side kept. In profit it takes 200 points."""
    assert (c400.LOTS_PER_LEG, c400.SHIFT_IN_LOSS, c400.SHIFT_IN_PROFIT) == (20, 150.0, 200.0)
    opened = (await market.check(at(4, 9, 30))).state["position"]
    put_side = {n: opened["legs"][n] for n in ("short_pe", "long_pe")}
    market.premium[(W1, PE, "PE")] = 400.0  # the sold weekly put dear: the position well in loss
    inside = await market.check(at(4, 10, 0), spot=23605.0)  # 145 up: not yet
    assert inside._last_action == "hold" and "shifts the call side over NIFTY 23,610" in inside._last_reason
    shifted = await market.check(at(4, 10, 1), spot=23615.0)  # 155 up: the call side shifted
    p = shifted.state["position"]
    assert shifted._last_signal == "SHIFT" and shifted._last_reason.startswith("strike shift, in loss")
    assert p["strike_ce"] > CE and p["legs"]["short_ce"]["strike"] == p["strike_ce"] and p["shifts"] == 1
    assert p["legs"]["short_ce"]["expiry"] == W1.isoformat() and p["legs"]["long_ce"]["expiry"] == opened["hedge_expiry"]
    assert p["legs"]["long_ce"]["strike"] == p["strike_ce"]
    assert {n: p["legs"][n] for n in ("short_pe", "long_pe")} == put_side  # the other side kept
    assert p["shift_ref_ce"] == 23615.0 and p["closed_legs"] == [] and p["booked"] == {"gross": 0.0, "charges": 0.0}
    [shift] = await market.trades()  # version 8: the closed legs are a trade of their own; the position carries on
    assert shift.exit_reason == "strike_shift" and [leg["side"] for leg in shift.legs] == ["short", "long"]
    assert shift.pnl == pytest.approx(p["last_shift_pnl"], abs=0.01) and shift.charges > 0
    assert [leg["side"] for leg in shifted.opened_legs] == ["sell", "buy"]
    assert all(leg["quantity"] == 20 * 65 for leg in p["legs"].values())
    again = await market.check(at(4, 10, 2), spot=23700.0)  # 85 over the new reference: held
    assert again._last_action == "hold" and again.state["position"]["shifts"] == 1


async def test_in_profit_short_of_the_target_it_takes_200_points_and_a_fall_shifts_the_put_side(market, monkeypatch):
    monkeypatch.setattr(c400, "TAKE_PROFIT", 10_000_000.0)  # in profit, never at the target here
    ctx = await market.check(at(4, 9, 30))
    ctx.state["position"]["booked"] = {"gross": 500_000.0, "charges": 0.0}  # earlier rolls booked well up: in profit
    market.deployment.state = ctx.state
    held = await market.check(at(4, 10, 0), spot=23290.0)  # 170 down: past 150 but in profit -- held
    assert held._last_action == "hold" and "200 points, in profit" in held._last_reason
    shifted = await market.check(at(4, 10, 1), spot=23250.0)  # 210 down: the put side shifted
    p = shifted.state["position"]
    assert shifted._last_signal == "SHIFT" and shifted._last_reason.startswith("strike shift, in profit")
    assert p["strike_pe"] < PE and p["strike_ce"] == CE and p["shift_ref_pe"] == 23250.0 and "shift_ref_ce" not in p


async def test_a_16_lot_position_from_version_5_is_shifted_at_its_own_16_lots(market):
    ctx = await market.check(at(4, 9, 30))
    for leg in ctx.state["position"]["legs"].values():
        leg["quantity"] = 16 * 65.0  # as version 5 opened it
    market.deployment.state = ctx.state
    market.premium[(W1, PE, "PE")] = 400.0
    shifted = await market.check(at(4, 10, 0), spot=23620.0)
    assert shifted._last_signal == "SHIFT" and all(leg["quantity"] == 16 * 65 for leg in shifted.state["position"]["legs"].values())


async def test_no_shift_outside_0930_1500(market):
    await market.check(at(4, 9, 30))
    market.premium[(W1, PE, "PE")] = 400.0
    late = await market.check(at(4, 15, 5), spot=23700.0)
    assert late._last_action == "hold" and late.state["position"].get("shifts") is None


async def test_a_shift_sends_a_strike_shifted_alert(market):
    await market.check(at(4, 9, 30))
    market.premium[(W1, PE, "PE")] = 400.0
    shifted = await market.check(at(4, 10, 0), spot=23620.0)
    title, body = entry_message("Conti Calendar 400 15 MIN", shifted.opened_legs, shifted.state["position"], shifted.now)
    lines = body.split("\n")
    assert title == "📅 Conti Calendar 400 15 MIN: strike shifted" and "Call side shifted" in lines
    assert any(line.startswith("Closed legs ") for line in lines) and max(len(line) for line in lines) <= WIDTH



async def _shifted_with_a_booked_loss(market, booked: float):
    """Opened, shifted on the call side in loss, then its booked result set to `booked` (left out of the target)."""
    await market.check(at(4, 9, 30))
    market.premium[(W1, PE, "PE")] = 400.0
    ctx = await market.check(at(4, 10, 0), spot=23620.0)
    assert ctx._last_signal == "SHIFT"
    p = ctx.state["position"]
    assert p["booked_outside_target"] == p["booked_net"]  # version 7: the target restarts at the shift
    p["booked"], p["booked_net"], p["booked_outside_target"] = {"gross": booked, "charges": 0.0}, booked, booked
    market.deployment.state = ctx.state
    market.premium.clear()
    return p


async def test_after_a_shift_the_target_restarts_at_14000_from_there(market):
    """20 Nov (version 7): after a shift the take-profit leaves out what was booked up to it."""
    p = await _shifted_with_a_booked_loss(market, -1_000_000.0)
    market.premium[(W1, p["strike_ce"], "CE")] = 1.0  # the new sold call nearly worthless: the legs held since well up
    out = await market.check(at(4, 10, 5), spot=23620.0)
    [trade] = [t for t in await market.trades() if t.exit_reason == "take_profit"]
    assert "since the last shift" in out._last_reason
    assert len(trade.legs) == 4  # the shift's two legs are in their own trade, not this one
    assert trade.pnl < -800_000  # the trade's own P&L is still the whole position's, the booked loss in it


async def test_after_a_shift_the_loss_or_profit_for_the_next_shift_counts_from_it(market, monkeypatch):
    monkeypatch.setattr(c400, "TAKE_PROFIT", 10_000_000.0)
    p = await _shifted_with_a_booked_loss(market, -1_000_000.0)
    p["booked"], p["booked_net"] = {"gross": -800_000.0, "charges": 0.0}, -800_000.0  # Rs 2 lakh booked since the shift (a roll)
    market.deployment.state = {**market.deployment.state, "position": p}  # in profit from the shift, the whole position deep in loss
    held = await market.check(at(4, 10, 5), spot=23790.0)  # 170 over the call side's new 23,620: under 200 -- held
    assert held._last_action == "hold" and "200 points, in profit" in held._last_reason and "since the last shift" in held._last_reason
    assert held.state["position"]["net_if_closed_now"] > 0  # from the shift: the roll's Rs 2 lakh and the legs held since
    again = await market.check(at(4, 10, 6), spot=23830.0)  # 210: shifted again, and the target restarts again
    q = again.state["position"]
    assert again._last_signal == "SHIFT" and q["shifts"] == 2 and q["booked_outside_target"] == q["booked_net"] == -800_000.0
    assert [t.exit_reason for t in await market.trades()] == ["strike_shift", "strike_shift"]


async def test_a_position_shifted_under_version_6_gets_the_restart_from_what_it_booked(market):
    p = await _shifted_with_a_booked_loss(market, -50_000.0)
    p.pop("booked_outside_target")  # as version 6 left it
    market.deployment.state = {**market.deployment.state, "position": p}
    held = await market.check(at(4, 10, 5), spot=23620.0)
    assert held.state["position"]["booked_outside_target"] == -50_000.0


async def test_the_shift_alert_says_the_target_restarts(market):
    await market.check(at(4, 9, 30))
    market.premium[(W1, PE, "PE")] = 400.0
    shifted = await market.check(at(4, 10, 0), spot=23620.0)
    _, body = entry_message("Conti Calendar 400 15 MIN", shifted.opened_legs, shifted.state["position"], shifted.now)
    assert "Target +14,000 from here" in body.split("\n")


async def test_a_shift_made_by_version_7_is_written_as_its_trade_once(market):
    """Version 8, on a position shifted before it (no roll since): the closed legs kept in it become a
    strike_shift trade at the shift's time, and Net if closed now counts from the shift."""
    ctx = await market.check(at(4, 9, 30))
    p = ctx.state["position"]
    legs = [{"instrument_id": p["legs"][n]["instrument_id"], "side": side, "quantity": p["legs"][n]["quantity"],
             "entry_price": price, "exit_price": out} for n, side, price, out in (("short_ce", "short", 105.9, 201.3), ("long_ce", "long", 314.15, 377.4))]
    shifted_at = at(4, 9, 50).astimezone(timezone.utc).isoformat()
    p.update(shifts=1, shifted_at=shifted_at, closed_legs=legs, booked={"gross": -33_436.0, "charges": 600.0}, booked_net=-34_036.0,
             booked_outside_target=-34_036.0)
    market.deployment.state = ctx.state
    after = await market.check(at(4, 10, 0))
    [trade] = await market.trades()
    assert trade.exit_reason == "strike_shift" and trade.pnl == -33_436.0 and len(trade.legs) == 2
    assert trade.closed_at.replace(tzinfo=timezone.utc).isoformat() == shifted_at
    q = after.state["position"]
    assert q["closed_legs"] == [] and q["booked_net"] == 0.0 and q["booked_outside_target"] == 0.0 and q["shift_trades"] is True
    await market.check(at(4, 10, 1))
    assert len(await market.trades()) == 1  # once


async def test_a_strike_shift_trade_sends_no_telegram_alert(market, monkeypatch):
    from app.services.notifications import trade_alerts

    sent = []

    async def fake_send(subject, body, **kw):
        sent.append(subject)

    monkeypatch.setattr(trade_alerts, "send_telegram", fake_send)
    monkeypatch.setattr(trade_alerts, "alerted_strategies", lambda: {"conti calendar 15 min", "conti calendar 400 15 min"})
    await market.check(at(4, 9, 30))
    market.premium[(W1, PE, "PE")] = 400.0
    shifted = await market.check(at(4, 10, 0), spot=23620.0)
    assert [t["exit_reason"] for t in shifted.closed_trades] == ["strike_shift"]
    await trade_alerts.send_trade_alerts(market.db, market.deployment, shifted, None)
    assert len(sent) == 1 and sent[0].endswith(": strike shifted")  # the shift's entry alert only, not its trade

