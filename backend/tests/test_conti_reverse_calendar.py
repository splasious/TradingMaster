"""Conti Reverse Calendar 15 MIN (native_strategies/nifty_conti_reverse_calendar.py,
11 Nov): Conti Calendar 15 MIN with the sides reversed -- the weekly CE and
PE bought, the same strike's monthly CE and PE sold, 16 lots a leg at bid /
ask -- everything else as Conti Calendar: the PCR's strike, Rs 14,000
take-profit after charges, the 15:29 close, a 5-minute pause after a close
and none after one from 14:00."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_conti_reverse_calendar as rev
from app.models.paper_trading import PaperNativeTrade
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_calendar import SPREAD, W1, W2, W3, Market, at, gain


class ReverseMarket(Market):
    async def check(self, now: datetime, spot: float = 23460.0) -> NativeContext:
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await rev.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await ReverseMarket(db_session).build(monkeypatch)
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def test_it_is_conti_calendar_with_the_sides_reversed():
    import app.services.strategy.native_strategies.nifty_conti_calendar as cal

    assert rev.LEGS == (("long_ce", "CE", "buy", "weekly"), ("long_pe", "PE", "buy", "weekly"),
                        ("short_ce", "CE", "sell", "monthly"), ("short_pe", "PE", "sell", "monthly"))
    for name in ("STRIKE_MULTIPLE", "CANDIDATE_STEPS", "LOTS_PER_LEG", "ENTRY_TIME", "ENTRY_UNTIL", "DAILY_CLOSE", "DAY_DONE_AFTER",
                 "RECHECK", "PAUSE_AFTER_CLOSE", "TAKE_PROFIT", "PCR_PIVOT", "PCR_TIMEFRAME"):
        assert getattr(rev, name) == getattr(cal, name), name
    assert rev.REGIME == "reverse calendar"


async def test_it_buys_the_weekly_and_sells_the_monthly_at_the_pcrs_strike_at_bid_and_ask(market):
    early = await market.check(at(4, 9, 29))
    assert early.state.get("position") is None and "outside entry window" in early._last_reason
    ctx = await market.check(at(4, 9, 30))
    p = ctx.state["position"]
    assert p["regime"] == "reverse calendar" and p["strike"] == 23450.0 and p["side"] == "call"  # PCR 0.9, as Conti Calendar picks
    legs = {name: (leg["side"], leg["option_type"], leg["expiry"]) for name, leg in p["legs"].items()}
    assert legs == {"long_ce": ("buy", "CE", W1.isoformat()), "long_pe": ("buy", "PE", W1.isoformat()),
                    "short_ce": ("sell", "CE", W3.isoformat()), "short_pe": ("sell", "PE", W3.isoformat())}
    weekly_ce = market.option(W1, 23450.0, "CE")
    assert p["legs"]["long_ce"]["entry_price"] == tick_engine.get_current_price(weekly_ce.id) + SPREAD  # bought at the ask
    monthly_ce = market.option(W3, 23450.0, "CE")
    assert p["legs"]["short_ce"]["entry_price"] == tick_engine.get_current_price(monthly_ce.id) - SPREAD  # sold at the bid
    assert all(leg["quantity"] == 16 * 65 for leg in p["legs"].values()) and ctx._last_signal == "CALENDAR"
    assert "reverse calendar opened at 23,450" in ctx._last_reason and "buy 09 Nov CE+PE" in ctx._last_reason


async def test_it_takes_the_profit_at_14000_after_charges_then_pauses(market):
    await market.check(at(4, 9, 30))
    held = await market.check(at(4, 10, 0))
    assert held._last_action == "hold" and held.state["position"]["net_if_closed_now"] < 0
    market.premium[(W3, 23450.0, "CE")] = market.premium[(W3, 23450.0, "PE")] = 150.0  # the sold monthlies fall
    done = await market.check(at(4, 11, 0))
    assert done.state.get("position") is None and "take-profit" in done._last_reason and done._wake_at == at(4, 11, 1)
    [trade] = await market.trades()
    assert trade.exit_reason == "take_profit" and len(trade.legs) == 4
    paused = await market.check(at(4, 11, 0, 30))  # a minute's pause
    assert paused.state.get("position") is None and "next entry from 11:01:00" in paused._last_reason


async def test_at_1529_in_profit_it_closes_in_loss_it_is_carried_and_no_new_trade_after_a_close_from_1400(market):
    """12 Nov (version 2, as Conti Calendar's version 11)."""
    p = (await market.check(at(4, 9, 30))).state["position"]
    carried = await market.check(at(4, 15, 29))  # in loss (the spreads and charges): carried
    assert carried.state["position"] is not None and "carried overnight" in carried._last_reason and await market.trades() == []
    gain(market, p, 3.0)
    closed = await market.check(at(5, 15, 29))
    assert closed.state.get("position") is None and closed._last_reason.startswith("15:29 close in profit")
    [trade] = await market.trades()
    assert trade.exit_reason == "time_cutoff_329pm"

    market.premium.clear()
    await market.check(at(6, 13, 0))
    market.deployment.state = {**market.deployment.state, "force_exit": True}
    await market.check(at(6, 14, 10))
    later = await market.check(at(6, 14, 30))
    assert later.state.get("position") is None and later._last_reason.startswith("no new trade today")


async def test_on_its_weekly_expiry_day_in_loss_it_is_rolled_into_a_new_one_and_in_profit_just_closes(market):
    p = (await market.check(at(4, 9, 30))).state["position"]
    await market.check(at(4, 15, 29))  # carried
    gain(market, p, -2.0)
    rolled = await market.check(at(9, 15, 0), spot=23560.0)
    [trade] = await market.trades()
    q = rolled.state["position"]
    assert trade.exit_reason == "expiry_roll" and rolled._last_reason.startswith("expiry roll: the 09 Nov weekly expiring")
    assert q["regime"] == "reverse calendar" and (q["weekly_expiry"], q["monthly_expiry"]) == (W2.isoformat(), W3.isoformat())
    assert {leg["side"] for name, leg in q["legs"].items() if name.startswith("long")} == {"buy"}  # still the weekly bought

    gain(market, q, 3.0)
    market.deployment.state = {**market.deployment.state, "position": {**q, "weekly_expiry": "2026-11-16"}}  # its weekly due: 16 Nov
    done = await market.check(at(16, 15, 0), spot=23560.0)
    assert done.state.get("position") is None and done._last_reason.startswith("the 16 Nov weekly expiring, in profit")
    assert [t.exit_reason for t in await market.trades()] == ["expiry_roll", "weekly_expiry_3pm"]


async def test_the_card_shows_it_at_bid_and_ask_with_its_1529_payoff_and_names_the_trade(market):
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.check(at(4, 9, 30))
    market.prices(at(4, 11, 0), 23460.0)
    out = await ep._build_position_out(market.db, market.deployment.state, at(4, 11, 0).astimezone(timezone.utc))
    assert out.bias == "reverse calendar"
    for leg in out.legs:
        assert leg.current_price == (leg.ask if leg.side == "short" else leg.bid)  # what closing fills at
    assert out.payoff is not None and out.payoff.horizon == at(4, 15, 29) and out.payoff.target == 14_000.0
    assert out.payoff.loss_cap is None  # a net credit: no debit caps the loss

    market.deployment.state = {**market.deployment.state, "force_exit": True}
    await market.check(at(4, 11, 0))
    rows = [(t, "Conti Reverse Calendar 15 MIN", "INR") for t in (await market.db.execute(select(PaperNativeTrade))).scalars().all()]
    [trade] = await ep._trade_outs(market.db, rows)
    assert trade.structure == "Reverse Straddle Calendar"


def test_its_trades_dont_go_to_telegram():
    from app.services.notifications.trade_alerts import alerted_strategies

    assert "conti reverse calendar 15 min" not in alerted_strategies()


async def test_a_wake_up_comes_a_second_on_while_it_is_open(market):
    opened = await market.check(at(4, 9, 30))
    assert opened._wake_at == at(4, 9, 30) + timedelta(seconds=1)
