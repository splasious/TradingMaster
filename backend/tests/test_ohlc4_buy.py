"""OHLC4 Buy 5 MIN (native_strategies/nifty_ohlc4_buy.py, 27 Nov): at each
5-minute close 09:20-15:25, the NIFTY candle just closed against the one
before's (open + high + low + close) / 4 -- above it one lot of the ATM
weekly call held, under it the put, flipped at once (sold at the bid, the
other bought 5 seconds later), the stop the candle before's low / high; no
trailing stop; held overnight, rolled at 12:00 on its expiry day; no
brokerage."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_ohlc4_buy as ob
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import PaperNativeTrade
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_calendar import SPREAD, W1, W2, Market, at


class OHLCMarket(Market):
    async def candle(self, start: datetime, o: float, h: float, low: float, c: float) -> None:
        """NIFTY's 5-minute candle opening at `start` (IST)."""
        self.db.add(OhlcvCandle(instrument_id=self.nifty.id, timeframe="5m", ts=start.astimezone(timezone.utc),
                                open=o, high=h, low=low, close=c, volume=0.0, source="test"))
        await self.db.commit()

    async def check(self, now: datetime, spot: float = 23460.0) -> NativeContext:
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await ob.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    def ltp(self, expiry, strike: float, option_type: str) -> float:
        return tick_engine.get_current_price(self.option(expiry, strike, option_type).id)


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await OHLCMarket(db_session).build(monkeypatch)
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


async def a_call_at_0920(market) -> None:
    await market.candle(at(3, 15, 25), 23400.0, 23500.0, 23300.0, 23450.0)  # the session before's last: level 23,412.50
    await market.candle(at(4, 9, 15), 23440.0, 23480.0, 23430.0, 23460.0)  # closes over it; its own level 23,452.50
    await market.check(at(4, 9, 20, 15), spot=23460.0)


def test_the_level_the_side_and_the_5_minute_closes():
    assert ob.level_of({"open": 23400, "high": 23500, "low": 23300, "close": 23450}) == 23412.5
    assert (ob.wanted_side(23420, 23412.5), ob.wanted_side(23400, 23412.5), ob.wanted_side(23412.5, 23412.5)) == ("CE", "PE", None)
    assert ob.latest_close(at(4, 9, 19)) == at(3, 15, 30)  # before 09:20: the session before's last candle
    assert ob.latest_close(at(4, 9, 20)) == at(4, 9, 20) and ob.latest_close(at(4, 10, 7)) == at(4, 10, 5)
    assert ob.latest_close(at(4, 16, 0)) == at(4, 15, 30)
    assert ob.next_decision(at(4, 9, 10)) == at(4, 9, 20) and ob.next_decision(at(4, 10, 7)) == at(4, 10, 10)
    assert ob.next_decision(at(4, 15, 24)) == at(4, 15, 25) and ob.next_decision(at(4, 15, 26)) == at(5, 9, 20)
    assert ob.next_decision(at(6, 15, 40)) == at(9, 9, 20)  # Friday's close: Monday's 09:20
    assert (ob.TIMEFRAME, ob.LOTS, ob.FLIP_GAP, ob.BROKERAGE_PER_ORDER) == ("5m", 1, timedelta(seconds=5), 0.0)
    assert not hasattr(ob, "TRAIL_START")  # no trailing stop


async def test_at_the_0920_close_above_the_candle_befores_level_it_buys_the_atm_weekly_call_at_the_ask(market):
    early = await market.check(at(4, 9, 16))
    assert early.state.get("position") is None and early._wake_at == at(4, 9, 20)
    await market.candle(at(3, 15, 25), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.candle(at(4, 9, 15), 23440.0, 23480.0, 23430.0, 23460.0)
    ctx = await market.check(at(4, 9, 20, 15), spot=23460.0)
    p = ctx.state["position"]
    assert (p["option"], p["option_type"], p["weekly_expiry"], p["bias"], p["regime"]) == (
        "09 Nov 23,450 CE", "CE", W1.isoformat(), "long call", "ohlc4 buy")
    assert (p["level"], p["stop"]) == (23452.5, 23430.0)  # the next close against the 09:15 candle: its level, its low
    leg = p["legs"]["long"]
    assert leg["quantity"] == 65 and leg["entry_price"] == market.ltp(W1, 23450.0, "CE") + SPREAD  # the ask
    assert ctx._last_signal == "BUY"
    assert "the 09:15 candle closed at 23,460.00 against the 03 Nov 15:25 candle's (O+H+L+C)/4 23,412.50" in ctx._last_reason
    assert ctx._wake_at == at(4, 9, 25) and ctx.state["brokerage_per_order"] == 0.0


async def test_between_closes_nothing_is_decided_and_a_late_candle_holds_what_is_held(market):
    await a_call_at_0920(market)
    between = await market.check(at(4, 9, 23), spot=23100.0)  # far under the level, but no candle has closed
    assert between.state["position"]["option_type"] == "CE" and between._wake_at == at(4, 9, 25)
    late = await market.check(at(4, 9, 25, 5), spot=23100.0)  # the 09:20 candle isn't in yet
    assert late.state["position"]["option_type"] == "CE" and "waiting for the NIFTY candle to 09:25" in late._last_reason
    assert late._wake_at == at(4, 9, 25, 6) and await market.trades() == []


async def test_a_close_under_the_level_flips_at_once_and_a_close_past_the_stop_is_recorded_as_the_stop(market):
    await a_call_at_0920(market)
    await market.candle(at(4, 9, 20), 23455.0, 23460.0, 23440.0, 23445.0)  # under 23,452.50, over the low 23,430
    sold = await market.check(at(4, 9, 25, 20), spot=23445.0)
    [trade] = await market.trades()
    assert trade.exit_reason == "level_flip" and sold.state.get("position") is None and sold._wake_at == at(4, 9, 25, 25)
    assert trade.legs[0]["exit_price"] == market.ltp(W1, 23450.0, "CE") - SPREAD  # the bid
    assert "now a put" in sold._last_reason
    bought = await market.check(at(4, 9, 25, 25), spot=23445.0)
    p = bought.state["position"]
    assert (p["option"], p["level"], p["stop"]) == ("09 Nov 23,450 PE", 23450.0, 23460.0)  # the 09:20 candle's level and high
    again = await market.check(at(4, 9, 26), spot=23445.0)  # the same candle isn't decided twice
    assert len(await market.trades()) == 1 and again.state["position"]["option_type"] == "PE"
    await market.candle(at(4, 9, 25), 23445.0, 23480.0, 23440.0, 23470.0)  # closes over the 09:20 candle's high
    stopped = await market.check(at(4, 9, 30, 20), spot=23470.0)
    assert [t.exit_reason for t in await market.trades()] == ["level_flip", "stop_loss"]
    assert stopped.state["pending"]["option_type"] == "CE" and "past its high: the stop" in stopped._last_reason


async def test_a_close_on_the_same_side_holds_and_moves_the_level_and_stop(market):
    await a_call_at_0920(market)
    await market.candle(at(4, 9, 20), 23460.0, 23500.0, 23455.0, 23490.0)  # over 23,452.50
    held = await market.check(at(4, 9, 25, 20), spot=23490.0)
    p = held.state["position"]
    assert await market.trades() == [] and "still a call" in held._last_reason
    assert (p["level"], p["stop"]) == (23476.25, 23455.0)


async def test_it_is_carried_overnight_and_rolled_at_noon_on_its_expiry_day_on_the_same_side(market):
    await market.candle(at(6, 14, 55), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.candle(at(6, 15, 0), 23440.0, 23480.0, 23430.0, 23460.0)
    p = (await market.check(at(6, 15, 5, 10), spot=23460.0)).state["position"]
    assert p["weekly_expiry"] == W1.isoformat()
    carried = await market.check(at(6, 15, 29))
    assert carried.state["position"] is not None and carried._last_reason.startswith("carried overnight:") and await market.trades() == []
    assert carried._wake_at == at(9, 9, 20)  # Monday's first close
    held = await market.check(at(9, 11, 59), spot=23460.0)
    assert "expires today: rolled to next week's at 12:00" in held._last_reason
    rolled = await market.check(at(9, 12, 0), spot=23460.0)
    assert [t.exit_reason for t in await market.trades()] == ["expiry_rollover"] and rolled._wake_at == at(9, 12, 0, 5)
    bought = await market.check(at(9, 12, 0, 5), spot=23400.0)
    p = bought.state["position"]
    assert (p["weekly_expiry"], p["option_type"], p["stop"]) == (W2.isoformat(), "CE", 23430.0)  # the side and the stop kept


async def test_its_trades_are_charged_no_brokerage(market):
    from app.services.paper_trading.trade_record import estimate_charges

    await a_call_at_0920(market)
    await market.candle(at(4, 9, 20), 23455.0, 23460.0, 23440.0, 23445.0)
    await market.check(at(4, 9, 25, 20), spot=23445.0)
    [trade] = (await market.db.execute(select(PaperNativeTrade))).scalars().all()
    assert trade.charges == estimate_charges(trade.legs, trade.opened_at, trade.closed_at, 0.0)
    assert estimate_charges(trade.legs, trade.opened_at, trade.closed_at) == pytest.approx(trade.charges + 2 * 20 * 1.18, abs=0.02)


def test_its_trades_dont_go_to_telegram():
    from app.services.notifications.trade_alerts import alerted_strategies

    assert not {"ohlc4 buy 5 min", "ohlc4 buy 15 min"} & set(alerted_strategies())
