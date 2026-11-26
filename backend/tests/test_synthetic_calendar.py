"""Synthetic Calendar 15 MIN and Synthetic Calendar Reverse 15 MIN
(native_strategies/nifty_synthetic_calendar*.py, 20 Nov): at the 50-point
strike nearest NIFTY, the weekly's CE bought and PE sold with the hedge's
PE bought and CE sold -- the reverse every leg the other way -- 20 lots a leg
at bid / ask; Rs 14,000 take-profit after charges, a minute's pause after a
close; all four closed at 15:00 on the weekly's expiry day; no stop-loss.
Since version 2 (26 Nov) the hedge is the weekly two weeks after the current
one at the same strike (the monthly after it before)."""

from datetime import date, datetime, timezone

import pytest

import app.services.strategy.native_strategies.nifty_synthetic_calendar as syn
import app.services.strategy.native_strategies.nifty_synthetic_calendar_reverse as rev
from app.models.instrument import Instrument
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_calendar import SPREAD, W1, W2, W3, Market, at


D1, D8 = date(2026, 12, 1), date(2026, 12, 8)  # version 2: weeklies after November's -- hedges two weeks out


class SyntheticMarket(Market):
    module = syn

    async def build(self, monkeypatch) -> "SyntheticMarket":
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

    async def check(self, now: datetime, spot: float = 23460.0) -> NativeContext:
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await self.module.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx


@pytest.fixture(params=[syn, rev], ids=["synthetic", "reverse"])
async def market(request, db_session, monkeypatch):
    m = await SyntheticMarket(db_session).build(monkeypatch)
    m.module = request.param
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def test_the_reverse_is_every_leg_the_other_way():
    flip = {"buy": "sell", "sell": "buy"}
    assert syn.LEGS == (("weekly_ce", "CE", "buy", "weekly"), ("weekly_pe", "PE", "sell", "weekly"),
                        ("hedge_pe", "PE", "buy", "hedge"), ("hedge_ce", "CE", "sell", "hedge"))
    assert rev.LEGS == tuple((n, t, flip[side], w) for n, t, side, w in syn.LEGS)
    for name in ("STRIKE_STEP", "LOTS_PER_LEG", "ENTRY_TIME", "ENTRY_UNTIL", "WEEKLY_CLOSE", "RECHECK", "PAUSE_AFTER_CLOSE",
                 "TAKE_PROFIT", "REGIME", "HEDGE_DAYS", "HEDGE_RULE", "EXPIRIES_READ"):
        assert getattr(rev, name) == getattr(syn, name), name
    assert (syn.LOTS_PER_LEG, syn.TAKE_PROFIT, syn.REGIME) == (20, 14_000.0, "synthetic calendar")
    assert (syn.NAME, rev.NAME) == ("Synthetic Calendar 15 MIN", "Synthetic Calendar Reverse 15 MIN")
    assert (syn.atm_strike(23460.0), syn.atm_strike(23475.0), syn.atm_strike(23474.9)) == (23450.0, 23500.0, 23450.0)


async def test_at_0930_all_four_legs_at_the_strike_nearest_nifty_at_bid_and_ask(market):
    early = await market.check(at(4, 9, 29))
    assert early.state.get("position") is None and "outside entry window" in early._last_reason
    ctx = await market.check(at(4, 9, 30))
    p = ctx.state["position"]
    assert p["regime"] == "synthetic calendar" and p["strike"] == 23450.0 and ctx._last_signal == "SYNTHETIC"
    assert (p["weekly_expiry"], p["hedge_expiry"]) == (W1.isoformat(), W3.isoformat())  # version 2: the weekly two weeks out
    assert p["hedge_rule"] == market.module.HEDGE_RULE and "monthly_expiry" not in p
    for name, option_type, side, which in market.module.LEGS:
        leg = p["legs"][name]
        expiry = W1 if which == "weekly" else W3
        ltp = tick_engine.get_current_price(market.option(expiry, 23450.0, option_type).id)
        assert (leg["side"], leg["option_type"], leg["expiry"], leg["strike"]) == (side, option_type, expiry.isoformat(), 23450.0)
        assert leg["entry_price"] == pytest.approx(ltp + SPREAD if side == "buy" else ltp - SPREAD)  # at the ask / the bid
        assert leg["quantity"] == 20 * 65
    assert [leg["side"] for leg in ctx.opened_legs] == [side for _, _, side, _ in market.module.LEGS]


async def test_it_takes_the_profit_at_14000_after_charges_then_pauses_a_minute(market):
    await market.check(at(4, 9, 30))
    held = await market.check(at(4, 10, 0))
    assert held._last_action == "hold" and held.state["position"]["net_if_closed_now"] < 14_000
    long_weekly_ce = market.module is syn
    market.premium[(W1, 23450.0, "CE")] = 600.0 if long_weekly_ce else 1.0  # the weekly CE: bought, it soars; sold, it collapses
    done = await market.check(at(4, 11, 0))
    [trade] = await market.trades()
    assert trade.exit_reason == "take_profit" and len(trade.legs) == 4 and done.state.get("position") is None
    paused = await market.check(at(4, 11, 0, 30))
    assert paused.state.get("position") is None and "next entry from 11:01:00" in paused._last_reason
    market.premium.clear()
    again = await market.check(at(4, 11, 1))
    assert again.state["position"] is not None


async def test_all_four_close_at_1500_on_the_weeklys_expiry_day_and_nothing_new(market):
    await market.check(at(4, 9, 30))
    before = await market.check(at(9, 14, 59, 59))
    assert before._last_action == "hold"
    closed = await market.check(at(9, 15, 0))
    [trade] = await market.trades()
    assert trade.exit_reason == "weekly_expiry_3pm" and closed.state.get("position") is None
    later = await market.check(at(9, 15, 2))
    assert later.state.get("position") is None and "outside entry window" in later._last_reason


async def test_on_an_expiry_day_it_trades_next_weeks_weekly(market):
    ctx = await market.check(at(9, 9, 30))
    assert ctx.state["position"]["weekly_expiry"] == W2.isoformat() and ctx.state["position"]["hedge_expiry"] == D1.isoformat()


def test_the_hedge_is_the_listed_expiry_two_weeks_after_the_weekly():
    """26 Nov (version 2)."""
    for module in (syn, rev):
        assert module.HEDGE_DAYS == (11, 17)
        assert module.hedge_after([W1, W2, W3, D1], W1) == W3  # 9 Nov (a Monday, 10 Nov a holiday) -> 23 Nov
        assert module.hedge_after([W1, W2, W3, D1], W2) == D1  # 17 Nov -> 1 Dec
        assert module.hedge_after([W2, W3, D8], W2) is None  # 1 Dec not listed: nothing two weeks on
        assert module.hedge_after([D1, date(2026, 12, 14)], D1) == date(2026, 12, 14)  # a holiday's Monday


async def test_with_no_expiry_listed_two_weeks_out_there_is_no_entry(market):
    market.weeklies = [W1, W2]
    ctx = await market.check(at(4, 9, 30))
    assert ctx.state.get("position") is None and ctx._last_reason == "no NIFTY expiry listed two weeks after 09 Nov yet for the hedge"


async def test_a_position_with_a_monthly_hedge_from_before_is_closed_and_reopened_by_the_new_rule(market):
    """26 Nov (version 2): the open version 1 position closes at the first check from 09:30 and the new shape opens at once."""
    old = (await market.check(at(4, 9, 30))).state["position"]
    old.pop("hedge_rule")
    old["monthly_expiry"] = old.pop("hedge_expiry")
    old["legs"] = {name.replace("hedge_", "monthly_"): leg for name, leg in old["legs"].items()}
    market.deployment.state = {**market.deployment.state, "position": old}
    night = await market.check(at(4, 16, 0))  # outside the entry window: left as it is
    assert night.state["position"].get("hedge_rule") is None and await market.trades() == []
    early = await market.check(at(5, 9, 20))  # before 09:30: held, read off its monthly's expiry
    assert early._last_action == "hold" and "hedge 23 Nov" in early._last_reason and await market.trades() == []
    ctx = await market.check(at(5, 9, 45))
    [trade] = await market.trades()
    p = ctx.state["position"]
    assert trade.exit_reason == "rule_change" and len(trade.legs) == 4 and p["hedge_rule"] == market.module.HEDGE_RULE
    assert ctx._last_signal == "SYNTHETIC" and ctx._last_reason.startswith("the hedge rule changed (version 2")
    assert (p["weekly_expiry"], p["hedge_expiry"]) == (W1.isoformat(), W3.isoformat()) and set(p["legs"]) == {
        "weekly_ce", "weekly_pe", "hedge_pe", "hedge_ce"}


async def test_a_position_left_past_its_weekly_closes_at_the_first_check(market):
    await market.check(at(4, 9, 30))
    late = await market.check(at(11, 9, 30))  # the app down on the 9th; 10 Nov an NSE holiday
    trades = await market.trades()
    assert trades[0].exit_reason == "weekly_expiry_3pm" and late.state.get("position") is None


async def test_its_trades_dont_go_to_telegram(market):
    from app.services.notifications.trade_alerts import alerted_strategies

    assert {syn.NAME.lower(), rev.NAME.lower()}.isdisjoint(alerted_strategies())
