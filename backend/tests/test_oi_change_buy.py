"""OI Change Buy 15 MIN (native_strategies/nifty_oi_change_buy.py, 16 Nov):
at every 15-minute close 09:30-15:15 the PCR record's change in OI over that
15-minute candle (version 2; the day's in version 1) --
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
        """The PCR Analysis tab's record for the close `mark`, a few seconds
        after it: `put_day` / `call_day` its last 15 minutes' change in OI
        (version 2) -- the day's change the other way round, so a rule on the
        day's would pick the other side."""
        self.db.add(PcrSnapshot(
            underlying="NIFTY", ts=mark.astimezone(timezone.utc), session_date=mark.date(),
            captured_at=(mark + timedelta(seconds=4)).astimezone(timezone.utc), source=SOURCE_LIVE, strike_window=40,
            expiries=[], put_oi_change=put_day, call_oi_change=call_day, put_oi_change_day=call_day, call_oi_change_day=put_day,
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


def test_the_side_and_the_strike():  # unwinding (a negative change) compared as it is
    assert oib.wanted_side(5_00_000, 2_00_000) == "CE" and oib.wanted_side(-1_00_000, 3_00_000) == "PE"
    assert oib.wanted_side(1.0, 1.0) is None and oib.wanted_side(None, 1.0) is None
    assert oib.wanted_side(-1_00_000, -4_00_000) == "CE" and oib.wanted_side(-4_00_000, -1_00_000) == "PE"  # both unwinding
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
    assert ctx._wake_at == at(4, 9, 30, 5)  # version 3: read every second while held, for the trail
    assert p["trail_starts_at"] == pytest.approx(leg["entry_price"] * 1.15, abs=0.01) and p["trail_stop"] is None


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
    assert bought.state["position"]["option_type"] == "PE" and bought._wake_at == at(4, 15, 15, 5)
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
    assert out.payoff is None and out.metrics["put_change_15m"] == 6_50_000

    market.deployment.state = {**market.deployment.state, "force_exit": True}
    manual = await market.check(at(4, 11, 0))
    assert manual.state.get("position") is None and manual._last_reason.startswith("manual exit")
    rows = [(t, "OI Change Buy 15 MIN", "INR") for t in (await market.db.execute(select(PaperNativeTrade))).scalars().all()]
    [trade] = await ep._trade_outs(market.db, rows)
    assert trade.structure == "Long CE"


async def test_version_2_reads_the_last_15_minutes_and_a_position_carried_over_keeps_on(market):
    """16 Nov, version 2: the 13:00 record's last 15 minutes had the call side
    higher (the day's still the put's): the call held is sold and a put bought."""
    await market.record(at(4, 11, 30), put_day=4_38_984, call_day=2_47_377)
    market.deployment.state = {"last_mark": at(4, 11, 15).isoformat()}
    held = (await market.check(at(4, 11, 30, 6))).state["position"]
    held.pop("put_change_15m"), held.pop("call_change_15m")
    market.deployment.state = {**market.deployment.state, "position": {**held, "put_change_day": 8.6e7, "call_change_day": 7.6e7}}
    await market.record(at(4, 11, 45), put_day=3_04_811, call_day=2_79_402)
    kept = (await market.check(at(4, 11, 45, 4))).state["position"]
    assert "put_change_day" not in kept and kept["put_change_15m"] == 3_04_811 and kept["option_type"] == "CE"
    await market.record(at(4, 12, 0), put_day=-7_76_295, call_day=36_63_530)
    sold = await market.check(at(4, 12, 0, 3))
    assert [t.exit_reason for t in await market.trades()] == ["oi_flip"] and "over the last 15 minutes" in sold._last_reason
    assert (await market.check(at(4, 12, 0, 8))).state["position"]["option_type"] == "PE"


async def test_the_trail_starts_at_plus_15_percent_then_sells_5_percent_under_the_highest_bid(market):
    """17 Nov (version 3): +15% / 5% on the bid, read every second; after it, flat until the next close."""
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    p = (await market.check(at(4, 9, 30, 4))).state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost * 1.14 + SPREAD  # a bid of +14%: not yet
    early = await market.check(at(4, 9, 35))
    assert early.state["position"]["trail_stop"] is None and "trail starts at a bid of" in early._last_reason
    market.premium[key] = cost * 1.30 + SPREAD  # +30%: on, the stop 5% under it
    on = (await market.check(at(4, 9, 36))).state["position"]
    assert on["peak_bid"] == pytest.approx(cost * 1.30, abs=0.01) and on["trail_stop"] == pytest.approx(cost * 1.30 * 0.95, abs=0.02)
    market.premium[key] = cost * 1.25 + SPREAD  # down a little, over the stop: held, the stop where it was
    held = (await market.check(at(4, 9, 37))).state["position"]
    assert held["trail_stop"] == on["trail_stop"] and await market.trades() == []
    market.premium[key] = cost * 1.22 + SPREAD  # under the stop: sold at the bid
    out = await market.check(at(4, 9, 38))
    [trade] = await market.trades()
    assert trade.exit_reason == "trailing_stop" and trade.legs[0]["exit_price"] == pytest.approx(cost * 1.22, abs=0.01)
    assert out.state.get("position") is None and out._last_reason.startswith("trailing stop:")
    again = await market.check(at(4, 9, 40))  # flat until the next close
    assert again.state.get("position") is None and "next decision at the 09:45 close" in again._last_reason
    await market.record(at(4, 9, 45), put_day=5_00_000, call_day=1_00_000)  # the same side again: bought again
    market.premium.clear()
    assert (await market.check(at(4, 9, 45, 4))).state["position"]["option_type"] == "CE"


async def test_a_stop_hit_at_a_close_waits_for_the_next_one(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    p = (await market.check(at(4, 9, 30, 4))).state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost * 1.40 + SPREAD
    await market.check(at(4, 9, 44))
    market.premium[key] = cost * 1.20 + SPREAD
    await market.record(at(4, 9, 45), put_day=5_00_000, call_day=1_00_000)
    out = await market.check(at(4, 9, 45, 2))  # the stop hit just after the 09:45 close: that close isn't bought on
    assert out.state.get("position") is None and out.state["last_mark"] == at(4, 9, 45).isoformat()
    assert (await market.check(at(4, 9, 45, 5))).state.get("position") is None


async def test_a_stop_loss_of_rs_1200_a_lot_until_the_trail_starts(market):
    """20 Nov (version 5): before the trail is on, sold at the bid once that loses Rs 1,200 a lot -- the
    price alone (18.46 points on a lot of 65), charges not counted; then flat until the next close."""
    assert oib.STOP_LOSS_PER_LOT == 1_200.0
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    bought = await market.check(at(4, 9, 30, 4))
    p = bought.state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    assert p["stop_loss_at"] == pytest.approx(cost - 1_200 / 65, abs=0.01) and "stop-loss at a bid of" in bought._last_reason
    market.premium[key] = cost - 18.40 + SPREAD  # Rs 1,196 down at the bid: held
    held = await market.check(at(4, 9, 35))
    assert held.state["position"] is not None and await market.trades() == []
    assert f"stop-loss at a bid of {cost - 1_200 / 65:,.2f}" in held._last_reason
    market.premium[key] = cost - 18.50 + SPREAD  # Rs 1,202.50 down: sold at the bid
    out = await market.check(at(4, 9, 36))
    [trade] = await market.trades()
    assert trade.exit_reason == "stop_loss" and trade.legs[0]["exit_price"] == pytest.approx(cost - 18.50, abs=0.01)
    assert trade.pnl == pytest.approx(-18.50 * 65, abs=1) and out.state.get("position") is None
    assert out._last_reason.startswith("stop-loss:")
    again = await market.check(at(4, 9, 40))  # flat until the next close
    assert again.state.get("position") is None and "next decision at the 09:45 close" in again._last_reason
    await market.record(at(4, 9, 45), put_day=5_00_000, call_day=1_00_000)
    market.premium.clear()
    assert (await market.check(at(4, 9, 45, 4))).state["position"]["option_type"] == "CE"


async def test_once_the_trail_is_on_the_stop_loss_no_longer_applies(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    p = (await market.check(at(4, 9, 30, 4))).state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost * 1.20 + SPREAD  # +20%: the trail on
    assert (await market.check(at(4, 9, 31))).state["position"]["trail_stop"] is not None
    market.premium[key] = cost - 30 + SPREAD  # far under cost: out by the trail, not the stop-loss
    await market.check(at(4, 9, 32))
    [trade] = await market.trades()
    assert trade.exit_reason == "trailing_stop"


async def test_a_position_from_version_4_gets_the_stop_loss_too(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    ctx = await market.check(at(4, 9, 30, 4))
    ctx.state["position"].pop("stop_loss_at")  # as version 4 left it
    market.deployment.state = ctx.state
    cost, key = ctx.state["position"]["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost - 25 + SPREAD
    await market.check(at(4, 9, 33))
    [trade] = await market.trades()
    assert trade.exit_reason == "stop_loss"


async def test_at_091550_the_first_minutes_change_in_oi_buys_before_0916(market, monkeypatch):
    """18 Nov (version 4): the live change in OI since the 09:15 record, read at 09:15:50."""
    readings = []

    async def reading(now_utc):
        readings.append(now_utc)
        return {"put_oi_change": 9_00_000.0, "call_oi_change": 4_00_000.0, "prev_ts": at(4, 9, 15).astimezone(timezone.utc)}, None

    monkeypatch.setattr(oib, "first_minute_reading", reading)
    market.deployment.state = {"last_mark": at(3, 15, 15).isoformat()}
    early = await market.check(at(4, 9, 15, 30))
    assert early.state.get("position") is None and early._wake_at == at(4, 9, 15, 50) and readings == []
    assert "next decision at 09:15:50" in early._last_reason
    ctx = await market.check(at(4, 9, 15, 50))
    p = ctx.state["position"]
    assert (p["option"], p["decided_at"]) == ("09 Nov 23,450 CE", "09:15 first minute") and ctx._last_signal == "BUY"
    assert "first minute's change in OI (since the 09:15 record): put +900,000, call +400,000 -- a call" in ctx._last_reason
    await market.check(at(4, 9, 15, 55))
    assert len(readings) == 1  # once a day
    await market.record(at(4, 9, 30), put_day=1_00_000, call_day=5_00_000)  # the 09:30 close reads its 15 minutes as usual
    await market.check(at(4, 9, 30, 4))
    assert [t.exit_reason for t in await market.trades()] == ["oi_flip"]


async def test_a_first_minute_buy_not_had_by_0916_waits_for_the_0930_close(market, monkeypatch):
    calls = []

    async def no_reading(now_utc):
        calls.append(now_utc)
        return None, "Zerodha isn't logged in"

    monkeypatch.setattr(oib, "first_minute_reading", no_reading)
    market.deployment.state = {"last_mark": at(3, 15, 15).isoformat()}
    waiting = await market.check(at(4, 9, 15, 50))
    assert waiting.state.get("position") is None and "no live change in OI yet" in waiting._last_reason
    assert waiting._wake_at == at(4, 9, 15, 51)
    late = await market.check(at(4, 9, 16, 0))
    assert late.state.get("position") is None and len(calls) == 1 and "next decision at the 09:30 close" in late._last_reason

    async def reading(now_utc):
        return {"put_oi_change": 9.0, "call_oi_change": 4.0, "prev_ts": None}, None

    monkeypatch.setattr(oib, "first_minute_reading", reading)
    market.deployment.state = {"last_mark": at(4, 15, 15).isoformat()}
    market.no_book = {(W1, 23450.0, "CE")}  # read, but no ask: not had by 09:16
    await market.check(at(5, 9, 15, 50))
    dropped = await market.check(at(5, 9, 16, 0))
    assert dropped.state.get("position") is None and dropped.state.get("pending") is None
    assert "wasn't had by 09:16 -- dropped" in dropped._last_reason


def test_its_trades_dont_go_to_telegram():
    from app.services.notifications.trade_alerts import alerted_strategies

    assert "oi change buy 15 min" not in alerted_strategies()
