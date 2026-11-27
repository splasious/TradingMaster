"""OHLC4 Buy 5 MIN (native_strategies/nifty_ohlc4_buy.py, 27 Nov): checked at
the 09:15 open (live NIFTY) and at each 5-minute close 09:20-15:25 (that
candle's close) against the previous 15-minute NIFTY candle's (open + high
+ low + close) / 4 -- above it one lot of the ATM weekly call held, under it
the put, flipped at once (sold at the bid, the other bought 5 seconds
later), the stop the 15-minute candle's low / high; no trailing stop; held
overnight, rolled at 12:00 on its expiry day; no brokerage."""

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
    async def candle(self, timeframe: str, start: datetime, o: float, h: float, low: float, c: float) -> None:
        """NIFTY's `timeframe` candle opening at `start` (IST)."""
        self.db.add(OhlcvCandle(instrument_id=self.nifty.id, timeframe=timeframe, ts=start.astimezone(timezone.utc),
                                open=o, high=h, low=low, close=c, volume=0.0, source="test"))
        await self.db.commit()

    async def five(self, start: datetime, close: float) -> None:
        await self.candle("5m", start, close, close + 5, close - 5, close)

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


async def a_call_at_the_open(market) -> NativeContext:
    await market.candle("15m", at(3, 15, 15), 23400.0, 23500.0, 23300.0, 23450.0)  # the session before's last: 23,412.50
    return await market.check(at(4, 9, 15, 2), spot=23460.0)


def test_the_level_the_side_and_the_checks():
    assert ob.level_of({"open": 23400, "high": 23500, "low": 23300, "close": 23450}) == 23412.5
    assert (ob.wanted_side(23420, 23412.5), ob.wanted_side(23400, 23412.5), ob.wanted_side(23412.5, 23412.5)) == ("CE", "PE", None)
    assert ob.latest_close(at(4, 9, 19)) == at(3, 15, 30) and ob.latest_close(at(4, 10, 7)) == at(4, 10, 5)
    level = (ob.LEVEL_FIRST_CLOSE, ob.LEVEL_BAR)
    assert ob.latest_close(at(4, 9, 25), *level) == at(3, 15, 30) and ob.latest_close(at(4, 10, 7), *level) == at(4, 10, 0)
    assert ob.next_decision(at(4, 9, 10)) == at(4, 9, 15) and ob.next_decision(at(4, 9, 16)) == at(4, 9, 20)
    assert ob.next_decision(at(4, 10, 7)) == at(4, 10, 10) and ob.next_decision(at(4, 15, 24)) == at(4, 15, 25)
    assert ob.next_decision(at(4, 15, 26)) == at(5, 9, 15) and ob.next_decision(at(6, 15, 40)) == at(9, 9, 15)  # Friday: Monday
    assert (ob.TIMEFRAME, ob.LEVEL_TIMEFRAME, ob.LOTS, ob.FLIP_GAP, ob.BROKERAGE_PER_ORDER) == (
        "5m", "15m", 1, timedelta(seconds=5), 0.0)
    assert not hasattr(ob, "TRAIL_START")  # no trailing stop


async def test_at_the_0915_open_above_the_level_it_buys_the_atm_weekly_call_at_the_ask(market):
    early = await market.check(at(4, 9, 14))
    assert early.state.get("position") is None and early._wake_at == at(4, 9, 15)
    ctx = await a_call_at_the_open(market)
    p = ctx.state["position"]
    assert (p["option"], p["option_type"], p["weekly_expiry"], p["bias"], p["regime"]) == (
        "09 Nov 23,450 CE", "CE", W1.isoformat(), "long call", "ohlc4 buy")
    assert (p["level"], p["stop"]) == (23412.5, 23300.0)  # the 15-minute candle's level and low
    leg = p["legs"]["long"]
    assert leg["quantity"] == 65 and leg["entry_price"] == market.ltp(W1, 23450.0, "CE") + SPREAD  # the ask
    assert ctx._last_signal == "BUY"
    assert "NIFTY 23,460.00 at the open against the 03 Nov 15:15 15-minute candle's (O+H+L+C)/4 23,412.50" in ctx._last_reason
    assert ctx._wake_at == at(4, 9, 20) and ctx.state["brokerage_per_order"] == 0.0
    again = await market.check(at(4, 9, 17), spot=23100.0)  # the open is checked once; nothing until 09:20
    assert again.state["position"]["option_type"] == "CE" and await market.trades() == [] and again._wake_at == at(4, 9, 20)


async def test_a_5_minute_close_under_the_level_flips_at_once_and_one_past_the_stop_is_recorded_as_the_stop(market):
    await a_call_at_the_open(market)
    await market.five(at(4, 9, 15), 23400.0)  # under 23,412.50, over the low 23,300
    sold = await market.check(at(4, 9, 20, 20), spot=23400.0)
    [trade] = await market.trades()
    assert trade.exit_reason == "level_flip" and sold.state.get("position") is None and sold._wake_at == at(4, 9, 20, 25)
    assert trade.legs[0]["exit_price"] == market.ltp(W1, 23450.0, "CE") - SPREAD  # the bid
    assert "the 09:15 5-minute candle closed at 23,400.00 against the 03 Nov 15:15 15-minute candle's" in sold._last_reason
    bought = await market.check(at(4, 9, 20, 25), spot=23400.0)
    assert (bought.state["position"]["option"], bought.state["position"]["stop"]) == ("09 Nov 23,400 PE", 23500.0)
    again = await market.check(at(4, 9, 22), spot=23600.0)  # between closes nothing is decided
    assert len(await market.trades()) == 1 and again.state["position"]["option_type"] == "PE"
    await market.five(at(4, 9, 20), 23510.0)  # closes over the 15-minute candle's high
    stopped = await market.check(at(4, 9, 25, 20), spot=23510.0)
    assert [t.exit_reason for t in await market.trades()] == ["level_flip", "stop_loss"]
    assert stopped.state["pending"]["option_type"] == "CE" and "past its high: the stop" in stopped._last_reason


async def test_a_late_candle_holds_and_each_15_minute_close_brings_a_new_level(market):
    await a_call_at_the_open(market)
    await market.five(at(3, 15, 25), 23450.0)  # the session before's last
    late = await market.check(at(4, 9, 20, 5), spot=23100.0)  # the 09:15 5-minute candle isn't in yet
    assert late.state["position"]["option_type"] == "CE" and "waiting for the NIFTY 5-minute candle to 09:20" in late._last_reason
    assert late._wake_at == at(4, 9, 20, 6)
    await market.five(at(4, 9, 15), 23440.0)
    await market.five(at(4, 9, 20), 23450.0)
    await market.five(at(4, 9, 25), 23420.0)
    waiting = await market.check(at(4, 9, 30, 15), spot=23420.0)  # the 09:15 15-minute candle isn't in yet
    assert "waiting for the NIFTY 15-minute candle to 09:30" in waiting._last_reason and await market.trades() == []
    await market.candle("15m", at(4, 9, 15), 23460.0, 23480.0, 23380.0, 23400.0)  # level 23,430
    moved = await market.check(at(4, 9, 30, 40), spot=23420.0)
    assert [t.exit_reason for t in await market.trades()] == ["level_flip"] and moved.state["pending"]["option_type"] == "PE"
    assert "against the 09:15 15-minute candle's (O+H+L+C)/4 23,430.00" in moved._last_reason


async def test_a_close_on_the_same_side_holds_and_moves_the_level_and_stop(market):
    await a_call_at_the_open(market)
    await market.five(at(4, 9, 25), 23470.0)
    await market.candle("15m", at(4, 9, 15), 23460.0, 23480.0, 23440.0, 23460.0)  # level 23,460, low 23,440
    held = await market.check(at(4, 9, 30, 20), spot=23470.0)
    p = held.state["position"]
    assert await market.trades() == [] and "still a call" in held._last_reason and (p["level"], p["stop"]) == (23460.0, 23440.0)


async def test_it_is_carried_overnight_and_rolled_at_noon_on_its_expiry_day_on_the_same_side(market):
    await market.candle("15m", at(6, 14, 45), 23400.0, 23500.0, 23300.0, 23450.0)
    await market.five(at(6, 15, 0), 23460.0)
    p = (await market.check(at(6, 15, 5, 10), spot=23460.0)).state["position"]
    assert (p["weekly_expiry"], p["option_type"]) == (W1.isoformat(), "CE")
    carried = await market.check(at(6, 15, 29))
    assert carried.state["position"] is not None and carried._last_reason.startswith("carried overnight:") and await market.trades() == []
    assert carried._wake_at == at(9, 9, 15)  # Monday's open
    held = await market.check(at(9, 11, 59), spot=23460.0)
    assert "expires today: rolled to next week's at 12:00" in held._last_reason
    rolled = await market.check(at(9, 12, 0), spot=23460.0)
    assert [t.exit_reason for t in await market.trades()] == ["expiry_rollover"] and rolled._wake_at == at(9, 12, 0, 5)
    bought = await market.check(at(9, 12, 0, 5), spot=23400.0)
    p = bought.state["position"]
    assert (p["weekly_expiry"], p["option_type"], p["stop"]) == (W2.isoformat(), "CE", 23300.0)  # the side and the stop kept


async def test_its_trades_are_charged_no_brokerage(market):
    from app.services.paper_trading.trade_record import estimate_charges

    await a_call_at_the_open(market)
    await market.five(at(4, 9, 15), 23400.0)
    await market.check(at(4, 9, 20, 20), spot=23400.0)
    [trade] = (await market.db.execute(select(PaperNativeTrade))).scalars().all()
    assert trade.charges == estimate_charges(trade.legs, trade.opened_at, trade.closed_at, 0.0)
    assert estimate_charges(trade.legs, trade.opened_at, trade.closed_at) == pytest.approx(trade.charges + 2 * 20 * 1.18, abs=0.02)


def test_its_trades_dont_go_to_telegram():
    from app.services.notifications.trade_alerts import alerted_strategies

    assert not {"ohlc4 buy 5 min", "ohlc4 buy 15 min"} & set(alerted_strategies())


async def test_the_card_chart_shows_the_5_minute_candles_and_the_15_minute_level_each_close_was_checked_against(market):
    from app.services.strategy import ohlc4_series as series

    assert series.is_charted(" OHLC4 Buy 5 MIN ") and not series.is_charted("OI Change Buy 15 MIN")
    await market.candle("15m", at(3, 15, 15), 23400.0, 23500.0, 23300.0, 23450.0)  # 23,412.50
    await market.five(at(3, 15, 25), 23450.0)  # the session before: not drawn
    for minute, close in ((15, 23400.0), (20, 23420.0), (25, 23470.0), (30, 23465.0)):
        await market.five(at(4, 9, minute), close)
    await market.candle("15m", at(4, 9, 15), 23460.0, 23480.0, 23440.0, 23460.0)  # 23,460, ends 09:30
    market.prices(at(4, 9, 37), 23468.0)
    out = await series.ohlc4_series(market.db, at(4, 9, 37).astimezone(timezone.utc))
    assert str(out["session"]) == "2026-11-04" and len(out["candles"]) == 4 and out["spot"] == 23468.0
    first, _, third, fourth = out["candles"]
    assert (first["level"], first["level_low"], first["level_high"], first["side"]) == (23412.5, 23300.0, 23500.0, "PE")
    assert (third["level"], third["side"]) == (23460.0, "CE")  # closed 09:30: the 09:15 15-minute candle by then
    assert (fourth["level"], fourth["side"], fourth["checked"]) == (23460.0, "CE", True)
    assert out["level_now"]["level"] == 23460.0 and out["level_now"]["level_low"] == 23440.0
    shut = await series.ohlc4_series(market.db, at(4, 16, 0).astimezone(timezone.utc))
    assert shut["spot"] is None and shut["level_now"] is None and len(shut["candles"]) == 4
