"""OI Change Buy 15 MIN (native_strategies/nifty_oi_change_buy.py, 16 Nov):
at every 15-minute close 09:30-15:15 the PCR record's day's change in OI --
put side higher, one lot of the ATM NIFTY call bought; call side higher,
the ATM put -- on the nearest weekly (next week's on its expiry day) at the
ask; the side flipping, the held option sold at the bid and the other bought
5 seconds later; everything sold at 15:29."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_oi_change_buy as oib
from app.models.paper_trading import PaperNativeTrade
from app.models.pcr import SOURCE_LIVE, PcrSnapshot
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_calendar import SPREAD, W1, W2, Market, at


class OIMarket(Market):
    async def record(self, mark: datetime, put_day: float | None, call_day: float | None) -> None:
        """The PCR Analysis tab's record for the close `mark`, a few seconds after it."""
        self.db.add(PcrSnapshot(
            underlying="NIFTY", ts=mark.astimezone(timezone.utc), session_date=mark.date(),
            captured_at=(mark + timedelta(seconds=4)).astimezone(timezone.utc), source=SOURCE_LIVE, strike_window=40,
            expiries=[], put_oi_change_day=put_day, call_oi_change_day=call_day,
        ))
        await self.db.commit()

    async def check(self, now: datetime, spot: float = 23460.0) -> NativeContext:
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await oib.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    def ltp(self, expiry, strike: float, option_type: str, now: datetime, spot: float) -> float:
        return tick_engine.get_current_price(self.option(expiry, strike, option_type).id)


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await OIMarket(db_session).build(monkeypatch)
    m.deployment.state = {"last_mark": at(4, 9, 15).isoformat()}  # a card already running before 09:30
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def test_the_side_and_the_strike():
    assert oib.wanted_side(5_00_000, 2_00_000) == "CE" and oib.wanted_side(-1_00_000, 3_00_000) == "PE"
    assert oib.wanted_side(1.0, 1.0) is None and oib.wanted_side(None, 1.0) is None
    assert (oib.atm_strike(23460.0), oib.atm_strike(23474.9), oib.atm_strike(23475.0)) == (23450.0, 23450.0, 23500.0)
    assert oib.latest_mark(at(4, 9, 29)) is None and oib.latest_mark(at(4, 9, 44, 59)) == at(4, 9, 30)
    assert oib.latest_mark(at(4, 15, 28)) == at(4, 15, 15) and oib.next_mark(at(4, 15, 15)) is None
    assert oib.next_mark(at(4, 9, 0)) == at(4, 9, 30) and oib.next_mark(at(4, 9, 30)) == at(4, 9, 45)
    assert (oib.LOTS, oib.FLIP_GAP, oib.DAY_CLOSE.strftime("%H:%M")) == (1, timedelta(seconds=5), "15:29")


async def test_at_0930_put_side_higher_it_buys_one_lot_of_the_atm_weekly_call_at_the_ask(market):
    early = await market.check(at(4, 9, 29))
    assert early.state.get("position") is None and early._wake_at == at(4, 9, 30)
    waiting = await market.check(at(4, 9, 30, 1))  # the record isn't in yet
    assert waiting.state.get("position") is None and "waiting for the 09:30 change-in-OI record" in waiting._last_reason
    assert waiting._wake_at == at(4, 9, 30, 2)
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    ctx = await market.check(at(4, 9, 30, 4))
    p = ctx.state["position"]
    assert (p["option"], p["option_type"], p["strike"], p["weekly_expiry"]) == ("09 Nov 23,450 CE", "CE", 23450.0, W1.isoformat())
    assert p["bias"] == "long call" and p["regime"] == "oi change buy" and p["decided_at"] == "09:30"
    leg = p["legs"]["long"]
    assert leg["quantity"] == 65 and leg["entry_price"] == market.ltp(W1, 23450.0, "CE", at(4, 9, 30, 4), 23460.0) + SPREAD  # the ask
    assert ctx._last_signal == "BUY" and "put +650,000, call +210,000" in ctx._last_reason
    assert ctx._wake_at == at(4, 9, 45)


async def test_the_same_side_keeps_the_option_held_and_a_flip_sells_it_then_buys_the_other_5_seconds_later(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    first = (await market.check(at(4, 9, 30, 4))).state["position"]
    await market.record(at(4, 9, 45), put_day=7_00_000, call_day=4_00_000)
    held = await market.check(at(4, 9, 45, 5), spot=23560.0)  # NIFTY has moved: the same contract held
    assert held.state["position"]["option"] == first["option"] and "still a call: held" in held._last_reason
    assert await market.trades() == []

    await market.record(at(4, 10, 0), put_day=3_00_000, call_day=9_00_000)  # the call side now higher
    sold = await market.check(at(4, 10, 0, 4), spot=23560.0)
    [trade] = await market.trades()
    assert trade.exit_reason == "oi_flip" and trade.legs[0]["exit_price"] == market.ltp(W1, 23450.0, "CE", at(4, 10, 0, 4), 23560.0) - SPREAD
    assert sold.state.get("position") is None and sold._wake_at == at(4, 10, 0, 9) and "the put bought at 10:00:09" in sold._last_reason
    between = await market.check(at(4, 10, 0, 6), spot=23560.0)
    assert between.state.get("position") is None and between._wake_at == at(4, 10, 0, 9)
    bought = await market.check(at(4, 10, 0, 9), spot=23560.0)
    p = bought.state["position"]
    assert (p["option"], p["bias"]) == ("09 Nov 23,550 PE", "long put")  # NIFTY 23,560's ATM now
    assert p["legs"]["long"]["entry_price"] == market.ltp(W1, 23550.0, "PE", at(4, 10, 0, 9), 23560.0) + SPREAD


async def test_an_equal_change_keeps_what_is_held_and_a_missing_record_is_passed_over_after_2_minutes(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    await market.check(at(4, 9, 30, 4))
    await market.record(at(4, 9, 45), put_day=5_00_000, call_day=5_00_000)
    equal = await market.check(at(4, 9, 45, 4))
    assert equal.state["position"]["option_type"] == "CE" and "no change" in equal._last_reason
    still = await market.check(at(4, 10, 1, 59))  # no 10:00 record yet
    assert "waiting for the 10:00" in still._last_reason
    passed = await market.check(at(4, 10, 2, 0))
    assert passed.state["position"]["option_type"] == "CE" and "passed over" in passed._last_reason
    assert await market.trades() == []


async def test_at_1529_it_is_sold_at_the_bid_and_nothing_more_is_bought_that_day(market):
    await market.record(at(4, 15, 15), put_day=1_00_000, call_day=4_00_000)
    market.deployment.state = {"last_mark": at(4, 15, 0).isoformat()}
    bought = await market.check(at(4, 15, 15, 4))
    assert bought.state["position"]["option_type"] == "PE" and bought._wake_at == at(4, 15, 29)
    closed = await market.check(at(4, 15, 29))
    [trade] = await market.trades()
    assert trade.exit_reason == "time_cutoff_329pm" and closed.state.get("position") is None
    assert trade.legs[0]["exit_price"] == market.ltp(W1, 23450.0, "PE", at(4, 15, 29), 23460.0) - SPREAD
    after = await market.check(at(4, 15, 30))
    assert after.state.get("position") is None and after._last_reason.startswith("day done")


async def test_a_position_left_from_an_earlier_day_is_sold_at_the_first_check(market):
    await market.record(at(4, 14, 0), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = {"last_mark": at(4, 13, 45).isoformat()}
    await market.check(at(4, 14, 0, 4))
    late = await market.check(at(5, 9, 20))  # the app was down at 15:29
    [trade] = await market.trades()
    assert trade.exit_reason == "time_cutoff_329pm" and late.state.get("position") is None


async def test_on_the_weeklys_expiry_day_it_buys_next_weeks(market):
    await market.record(at(9, 9, 30), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = {"last_mark": at(6, 15, 15).isoformat()}
    ctx = await market.check(at(9, 9, 30, 4))
    assert ctx.state["position"]["weekly_expiry"] == W2.isoformat() and ctx.state["position"]["option"] == "17 Nov 23,450 CE"


async def test_a_card_started_between_closes_begins_at_the_next_one_and_a_buy_waits_for_a_book(market):
    await market.record(at(4, 10, 0), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = None  # a new card, started 10:07
    fresh = await market.check(at(4, 10, 7))
    assert fresh.state.get("position") is None and "next decision at the 10:15 close" in fresh._last_reason
    await market.record(at(4, 10, 15), put_day=4_00_000, call_day=1_00_000)
    market.no_book = {(W1, 23450.0, "CE")}
    waits = await market.check(at(4, 10, 15, 4))
    assert waits.state.get("position") is None and "no bid/ask for the 09 Nov 23,450 CE yet" in waits._last_reason
    assert waits._wake_at == at(4, 10, 15, 5)
    market.no_book = set()
    assert (await market.check(at(4, 10, 15, 5))).state["position"]["option"] == "09 Nov 23,450 CE"


async def test_the_card_values_the_option_at_the_bid_and_names_the_trade(market):
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    await market.check(at(4, 9, 30, 4))
    market.prices(at(4, 11, 0), 23460.0)
    out = await ep._build_position_out(market.db, market.deployment.state, at(4, 11, 0).astimezone(timezone.utc))
    [leg] = out.legs
    assert out.bias == "long call" and leg.current_price == leg.bid and leg.ask == leg.bid + 2 * SPREAD
    assert out.payoff is None and out.metrics["put_change_day"] == 6_50_000

    market.deployment.state = {**market.deployment.state, "force_exit": True}
    manual = await market.check(at(4, 11, 0))
    assert manual.state.get("position") is None and manual._last_reason.startswith("manual exit")
    rows = [(t, "OI Change Buy 15 MIN", "INR") for t in (await market.db.execute(select(PaperNativeTrade))).scalars().all()]
    [trade] = await ep._trade_outs(market.db, rows)
    assert trade.structure == "Long CE"


def test_its_trades_dont_go_to_telegram():
    from app.services.notifications.trade_alerts import alerted_strategies

    assert "oi change buy 15 min" not in alerted_strategies()
