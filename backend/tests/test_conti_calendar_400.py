"""Conti Calendar 400 15 MIN (native_strategies/nifty_conti_calendar_400.py,
11 Nov): Conti Calendar 15 MIN copied as a double calendar -- the weekly CE
sold and the monthly CE bought at ATM+400, the weekly PE sold and the
monthly PE bought at ATM-400, strikes in hundreds, 16 lots a leg at bid /
ask -- held overnight, the sold weeklies rolled at 15:00 on their expiry
day (all four reopened on next month's monthly in the month's last week),
closed at Rs 14,000 after charges, every trade keeping the most profit and
loss it reached."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_conti_calendar_400 as c400
from app.models.paper_trading import PaperNativeTrade
from app.services.market_data.tick_engine import tick_engine
from app.services.notifications.trade_alerts import entry_message, exit_message
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_calendar import DEC, SPREAD, W1, W2, W3, Market, at

WIDTH = 32  # the Telegram group's message block


class Market400(Market):
    """Conti Calendar's test market, run with the double calendar; options
    priced so the ones 400 away have a premium: 30 a week to expiry plus
    what's in the money."""

    def prices(self, now: datetime, spot: float) -> None:
        tick_engine.set_real_price(self.nifty.id, spot, source="test")
        tick_engine._real_price_at[self.nifty.id] = now
        for o in self.options:
            weeks = max(1, (o.expiry - now.date()).days / 7)
            intrinsic = max(0.0, spot - o.strike) if o.option_type == "CE" else max(0.0, o.strike - spot)
            ltp = self.premium.get((o.expiry, o.strike, o.option_type), 30 * weeks + intrinsic)
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


def test_the_strikes_are_400_either_side_of_nifty_to_the_nearest_100():
    assert c400.atm_of(24630.0) == 24600.0 and c400.strikes_for(24630.0) == {"CE": 25000.0, "PE": 24200.0}
    assert c400.atm_of(24650.0) == 24700.0 and c400.atm_of(24649.9) == 24600.0  # a half rounds up
    assert c400.strikes_for(23460.0) == {"CE": 23900.0, "PE": 23100.0}


async def test_it_opens_a_double_calendar_at_0930_at_bid_and_ask(market):
    early = await market.check(at(4, 9, 29))
    assert early.state.get("position") is None and "outside entry window" in early._last_reason
    ctx = await market.check(at(4, 9, 30))
    p = ctx.state["position"]
    assert p["regime"] == "double calendar" and (p["strike_ce"], p["strike_pe"], p["atm"]) == (23900.0, 23100.0, 23500.0)
    assert _legs(p) == {
        "short_ce": ("sell", "CE", 23900.0, W1.isoformat()), "long_ce": ("buy", "CE", 23900.0, W3.isoformat()),
        "short_pe": ("sell", "PE", 23100.0, W1.isoformat()), "long_pe": ("buy", "PE", 23100.0, W3.isoformat()),
    }
    monthly = 30 * (W3 - date(2026, 11, 4)).days / 7  # the test market's monthly premium 400 out
    assert p["legs"]["short_ce"]["entry_price"] == 30.0 - SPREAD and p["legs"]["long_ce"]["entry_price"] == pytest.approx(monthly + SPREAD)
    assert all(leg["quantity"] == 16 * 65 for leg in p["legs"].values())
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
    market.premium[(W1, 23900.0, "CE")] = market.premium[(W1, 23900.0, "PE")] = 10.0  # untouched strikes: the default prices
    market.premium[(W1, 23100.0, "PE")] = 60.0  # NIFTY dips toward the put: a loss for now
    dip = await market.check(at(4, 11, 0))
    low = dip.state["position"]["max_loss_reached"]
    assert low < p["max_loss_reached"] and dip.state["position"]["max_loss_at"] == "04 Nov 11:00:00 IST"
    market.premium[(W1, 23100.0, "PE")] = market.premium[(W1, 23900.0, "CE")] = 1.0  # both sold weeklies collapse
    done = await market.check(at(4, 13, 0))
    assert done.state.get("position") is None and done._last_signal == "COVER" and "take-profit" in done._last_reason
    assert "max loss reached" in done._last_reason and done._wake_at == at(4, 13, 5)
    [trade] = await market.trades()
    assert trade.exit_reason == "take_profit" and len(trade.legs) == 4
    e = trade.extremes
    assert e["max_profit_reached"] >= 14_000 and e["max_profit_at"] == "04 Nov 13:00:00 IST"
    assert e["max_loss_reached"] == low and e["max_loss_at"] == "04 Nov 11:00:00 IST"

    paused = await market.check(at(4, 13, 4, 59))
    assert paused.state.get("position") is None and "next entry from 13:05:00" in paused._last_reason
    market.premium.clear()
    again = await market.check(at(4, 13, 5))
    assert again.state["position"] is not None and "max_profit_reached" not in again.state["position"]


async def test_the_sold_weeklies_roll_at_1500_on_their_expiry_at_the_same_strikes(market):
    await market.check(at(4, 9, 30))
    before = await market.check(at(9, 14, 59, 59))
    assert before.state["position"]["weekly_expiry"] == W1.isoformat()
    rolled = await market.check(at(9, 15, 0), spot=23700.0)  # NIFTY has moved: the strikes don't
    p = rolled.state["position"]
    assert rolled._last_signal == "ROLL" and "weekly roll 09 Nov -> 17 Nov at the same strikes" in rolled._last_reason
    assert _legs(p) == {
        "short_ce": ("sell", "CE", 23900.0, W2.isoformat()), "long_ce": ("buy", "CE", 23900.0, W3.isoformat()),
        "short_pe": ("sell", "PE", 23100.0, W2.isoformat()), "long_pe": ("buy", "PE", 23100.0, W3.isoformat()),
    }
    assert p["rolls"] == 1 and p["weekly_expiry"] == W2.isoformat() and p["monthly_expiry"] == W3.isoformat()
    assert len(p["closed_legs"]) == 2 and {leg["side"] for leg in p["closed_legs"]} == {"short"}
    assert p["booked"]["charges"] > 0 and p["booked_net"] == pytest.approx(p["booked"]["gross"] - p["booked"]["charges"], abs=0.01)
    assert [leg["side"] for leg in rolled.opened_legs] == ["sell", "sell"]
    assert await market.trades() == []  # a roll isn't a closed trade: the position carries on
    held = await market.check(at(11, 10, 0), spot=23700.0)
    assert held._last_action == "hold" and held.state["position"]["rolls"] == 1


async def test_a_roll_missed_while_the_app_was_down_happens_at_the_first_check_after(market):
    await market.check(at(4, 9, 30))
    late = await market.check(at(11, 9, 30))  # 10 Nov is an NSE holiday (Diwali Balipratipada)
    assert late._last_signal == "ROLL" and late.state["position"]["weekly_expiry"] == W2.isoformat()


async def test_in_the_months_last_week_all_four_close_and_reopen_on_next_months_monthly(market):
    await market.check(at(4, 9, 30))
    await market.check(at(9, 15, 0))  # the first roll, to 17 Nov
    reopened = await market.check(at(17, 15, 0), spot=23560.0)  # next week's is November's monthly itself
    [trade] = await market.trades()
    assert trade.exit_reason == "monthly_roll" and len(trade.legs) == 6  # the two rolled weeklies and the four closed now
    assert trade.extremes is not None
    p = reopened.state["position"]
    assert reopened._last_signal == "CALENDAR" and reopened._last_reason.startswith("monthly roll: all four closed")
    assert (p["strike_ce"], p["strike_pe"]) == (24000.0, 23200.0)  # the new ATM 23,600 +-400
    assert (p["weekly_expiry"], p["monthly_expiry"]) == (W3.isoformat(), DEC.isoformat()) and p["rolls"] == 0
    assert "reopen_due" not in reopened.state and "paused_until" not in reopened.state


async def test_a_reopen_that_cant_be_had_is_tried_again_whatever_the_time(market):
    await market.check(at(4, 9, 30))
    await market.check(at(9, 15, 0))
    market.no_book = {(DEC, 24000.0, "CE")}
    waiting = await market.check(at(17, 15, 0), spot=23560.0)
    assert waiting.state.get("position") is None and waiting.state.get("reopen_due") and "no bid/ask" in waiting._last_reason
    market.no_book = set()
    later = await market.check(at(17, 15, 10), spot=23560.0)  # past 15:00 and inside the pause: still reopened
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
    assert lines[0] == "Double Calendar" and "23,900 CE / 23,100 PE" in lines and "16 lots a leg" in lines
    assert "Target +14,000 after costs" in lines and "Held overnight; weeklies" in lines
    assert max(len(line) for line in lines) <= WIDTH

    rolled = await market.check(at(9, 15, 0))
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
