"""OHLC4 Buy 15 MIN (native_strategies/nifty_ohlc4_buy.py, 27 Nov): from the
09:15 open to 15:29, live NIFTY against the previous 15-minute candle's
(open + high + low + close) / 4 -- above it one lot of the ATM weekly call,
under it the put, flipped at once (sold at the bid, the other bought 5
seconds later), the stop the previous candle's low / high; no trailing stop;
held overnight, rolled at 12:00 on its expiry day; no brokerage."""

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
        """NIFTY's 15-minute candle opening at `start` (IST)."""
        self.db.add(OhlcvCandle(instrument_id=self.nifty.id, timeframe="15m", ts=start.astimezone(timezone.utc),
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


def test_the_level_the_side_and_the_previous_candle():
    assert ob.level_of({"open": 23400, "high": 23500, "low": 23300, "close": 23450}) == 23412.5
    assert (ob.wanted_side(23420, 23412.5), ob.wanted_side(23400, 23412.5), ob.wanted_side(23412.5, 23412.5)) == ("CE", "PE", None)
    assert ob.previous_end(at(4, 9, 20)) == at(3, 15, 30)  # before 09:30: the session before's last candle
    assert ob.previous_end(at(4, 9, 30)) == at(4, 9, 30) and ob.previous_end(at(4, 10, 7)) == at(4, 10, 0)
    assert ob.previous_end(at(4, 15, 29)) == at(4, 15, 15) and ob.previous_end(at(4, 16, 0)) == at(4, 15, 30)
    assert (ob.LOTS, ob.FLIP_GAP, ob.BROKERAGE_PER_ORDER, ob.SESSION_OPEN.strftime("%H:%M")) == (1, timedelta(seconds=5), 0.0, "09:15")
    assert not hasattr(ob, "TRAIL_START")  # no trailing stop


async def test_at_the_0915_open_above_the_previous_candles_level_it_buys_the_atm_weekly_call_at_the_ask(market):
    early = await market.check(at(4, 9, 14))
    assert early.state.get("position") is None and early._wake_at == at(4, 9, 15)
    await market.candle(at(3, 15, 15), 23400.0, 23500.0, 23300.0, 23450.0)  # the session before's last: level 23,412.50
    ctx = await market.check(at(4, 9, 15, 1), spot=23460.0)
    p = ctx.state["position"]
    assert (p["option"], p["option_type"], p["weekly_expiry"], p["bias"]) == ("09 Nov 23,450 CE", "CE", W1.isoformat(), "long call")
    assert (p["level"], p["stop"], p["regime"]) == (23412.5, 23300.0, "ohlc4 buy")  # its stop the previous candle's low
    leg = p["legs"]["long"]
    assert leg["quantity"] == 65 and leg["entry_price"] == market.ltp(W1, 23450.0, "CE") + SPREAD  # the ask
    assert ctx._last_signal == "BUY" and "NIFTY 23,460.00 against the 03 Nov 15:15 candle's (O+H+L+C)/4 23,412.50" in ctx._last_reason
    assert ctx._wake_at == at(4, 9, 15, 2) and ctx.state["brokerage_per_order"] == 0.0


async def test_under_the_level_it_flips_at_once_and_a_cross_past_the_stop_is_recorded_as_the_stop(market):
    await market.candle(at(3, 15, 15), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.check(at(4, 9, 15, 1), spot=23460.0)
    held = await market.check(at(4, 9, 16), spot=23420.0)  # still over 23,412.50
    assert held.state["position"]["option_type"] == "CE" and "still a call" in held._last_reason
    sold = await market.check(at(4, 9, 17), spot=23400.0)  # under the level, over the low: a flip
    [trade] = await market.trades()
    assert trade.exit_reason == "level_flip" and sold.state.get("position") is None and sold._wake_at == at(4, 9, 17, 5)
    assert trade.legs[0]["exit_price"] == market.ltp(W1, 23450.0, "CE") - SPREAD  # the bid
    bought = await market.check(at(4, 9, 17, 5), spot=23400.0)
    p = bought.state["position"]
    assert (p["option"], p["stop"]) == ("09 Nov 23,400 PE", 23500.0)  # the put's stop: the previous candle's high
    gap = await market.check(at(4, 9, 18), spot=23520.0)  # a jump past the high: the put sold, recorded as the stop
    assert [t.exit_reason for t in await market.trades()] == ["level_flip", "stop_loss"] and gap.state["pending"]["option_type"] == "CE"
    assert "past its high: the stop" in gap._last_reason


async def test_each_new_candle_moves_the_level_and_a_late_candle_holds_what_is_held(market):
    await market.candle(at(3, 15, 15), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.check(at(4, 9, 15, 1), spot=23460.0)
    late = await market.check(at(4, 9, 30, 5), spot=23300.0)  # the 09:15 candle isn't in: no decision
    assert late.state["position"]["option_type"] == "CE" and "waiting for the NIFTY candle to 09:30" in late._last_reason
    assert await market.trades() == []
    await market.candle(at(4, 9, 15), 23460.0, 23480.0, 23250.0, 23280.0)  # level 23,367.50, low 23,250
    moved = await market.check(at(4, 9, 30, 6), spot=23300.0)
    assert [t.exit_reason for t in await market.trades()] == ["level_flip"] and moved.state["pending"]["option_type"] == "PE"
    assert "the 09:15 candle's (O+H+L+C)/4 23,367.50" in moved._last_reason


async def test_a_buy_takes_the_side_nifty_is_on_when_it_falls_due(market):
    await market.candle(at(3, 15, 15), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.check(at(4, 9, 15, 1), spot=23460.0)
    await market.check(at(4, 9, 17), spot=23400.0)  # the call sold, a put due 09:17:05
    back = await market.check(at(4, 9, 17, 3), spot=23430.0)  # back over the level inside the 5 seconds
    assert back.state["pending"]["option_type"] == "CE"
    assert (await market.check(at(4, 9, 17, 5), spot=23430.0)).state["position"]["option_type"] == "CE"


async def test_it_is_carried_overnight_and_rolled_at_noon_on_its_expiry_day(market):
    await market.candle(at(6, 15, 0), 23400.0, 23500.0, 23300.0, 23450.0)
    p = (await market.check(at(6, 15, 15, 1), spot=23460.0)).state["position"]
    assert p["weekly_expiry"] == W1.isoformat()
    carried = await market.check(at(6, 15, 29))
    assert carried.state["position"] is not None and carried._last_reason.startswith("carried overnight:") and await market.trades() == []
    assert carried._wake_at == at(9, 9, 15)  # Monday's open
    await market.candle(at(6, 15, 15), 23440.0, 23470.0, 23420.0, 23450.0)  # level 23,445
    await market.candle(at(9, 11, 45), 23440.0, 23470.0, 23420.0, 23450.0)
    held = await market.check(at(9, 11, 59), spot=23460.0)
    assert "expires today: rolled to next week's at 12:00" in held._last_reason
    rolled = await market.check(at(9, 12, 0), spot=23460.0)
    assert [t.exit_reason for t in await market.trades()] == ["expiry_rollover"] and rolled._wake_at == at(9, 12, 0, 5)
    await market.candle(at(9, 12, 0), 23440.0, 23470.0, 23420.0, 23450.0)
    bought = await market.check(at(9, 12, 0, 5), spot=23460.0)
    assert (bought.state["position"]["weekly_expiry"], bought.state["position"]["option_type"]) == (W2.isoformat(), "CE")


async def test_a_rollover_buys_the_side_nifty_is_on_when_it_falls_due(market):
    await market.candle(at(9, 11, 30), 23400.0, 23500.0, 23300.0, 23450.0)  # level 23,412.50
    await market.check(at(9, 11, 45, 1), spot=23460.0)  # a call on 09 Nov, its expiry day: a next-week's bought
    market.deployment.state["position"]["weekly_expiry"] = W1.isoformat()  # as if bought before today
    await market.candle(at(9, 11, 45), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.check(at(9, 12, 0), spot=23460.0)
    assert [t.exit_reason for t in await market.trades()] == ["expiry_rollover"]
    bought = await market.check(at(9, 12, 0, 5), spot=23400.0)  # under the level by the time the buy falls due
    assert (bought.state["position"]["weekly_expiry"], bought.state["position"]["option_type"]) == (W2.isoformat(), "PE")


async def test_its_trades_are_charged_no_brokerage(market):
    from app.services.paper_trading.trade_record import estimate_charges

    await market.candle(at(3, 15, 15), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.check(at(4, 9, 15, 1), spot=23460.0)
    await market.check(at(4, 9, 17), spot=23400.0)
    [trade] = (await market.db.execute(select(PaperNativeTrade))).scalars().all()
    assert trade.charges == estimate_charges(trade.legs, trade.opened_at, trade.closed_at, 0.0)
    assert estimate_charges(trade.legs, trade.opened_at, trade.closed_at) == pytest.approx(trade.charges + 2 * 20 * 1.18, abs=0.02)


def test_its_trades_dont_go_to_telegram():
    from app.services.notifications.trade_alerts import alerted_strategies

    assert "ohlc4 buy 15 min" not in alerted_strategies()
