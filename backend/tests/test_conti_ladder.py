"""Conti Ladder Straddle 15 MIN (native_strategies/nifty_conti_ladder.py, 22
Oct): five NIFTY short straddles, 2 lots a leg, at the PCR-picked middle
strike and 100 and 200 points either side, held from 09:45 to 15:00 and
shifted only when a finished 15-minute close is 100 points from entry."""

from datetime import timedelta, timezone

import pytest

import app.services.strategy.native_strategies.nifty_conti_ladder as ladder
from app.models.market_data import OhlcvCandle
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_straddle import Market, at


class Ladder(Market):
    async def check(self, now, spot, pcr):
        self.pcr = pcr
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await ladder.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    async def candle(self, hh: int, mm: int, close: float) -> None:
        """NIFTY's finished 15-minute candle opening at hh:mm."""
        self.db.add(OhlcvCandle(instrument_id=self.nifty.id, timeframe="15m", ts=at(hh, mm).astimezone(timezone.utc), open=close,
                                high=close + 5, low=close - 5, close=close, volume=None, source="test"))
        await self.db.commit()


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Ladder(db_session).build(monkeypatch)
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def ladder_of(ctx) -> tuple[float, list[float], set[float]]:
    p = ctx.state["position"]
    return p["strike"], p["strikes"], {leg["quantity"] for leg in p["legs"].values()}


def test_the_ladder_is_the_middle_strike_and_100_and_200_either_side():
    assert ladder.ladder_strikes(23450) == [23250, 23350, 23450, 23550, 23650]
    assert ladder.LOTS_PER_LEG * len(ladder.LADDER) == 10  # 10 lots a side in all, as Conti's one straddle


async def test_it_opens_five_two_lot_straddles_around_the_pcr_picked_strike(market):
    early = await market.check(at(9, 40), 23460.0, 0.92)
    assert early.state.get("position") is None and "outside entry window" in early._last_reason
    opened = await market.check(at(9, 45, 5), 23460.0, 0.92)  # call-heavy: 23,450 (as Conti picks it)
    assert ladder_of(opened) == (23450, [23250, 23350, 23450, 23550, 23650], {130.0})
    legs = opened.state["position"]["legs"]
    assert len(legs) == 10 and {(leg["strike"], leg["option_type"]) for leg in legs.values()} == {
        (k, t) for k in (23250, 23350, 23450, 23550, 23650) for t in ("CE", "PE")}
    assert opened._last_signal == "STRADDLE" and "opened at 23,250-23,650" in opened._last_reason


async def test_it_shifts_only_when_a_finished_candle_closes_100_points_away(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    await market.candle(9, 30, 23600.0)  # closed before the ladder opened: doesn't count
    live_jump = await market.check(at(10, 5), 23600.0, 1.08)  # 140 points live, no candle closed since
    assert ladder_of(live_jump)[0] == 23450 and await market.trades() == []
    assert "no 15-min candle closed since it opened" in live_jump._last_reason

    await market.candle(9, 45, 23559.0)  # 99 points: holds
    held = await market.check(at(10, 0, 20), 23600.0, 1.08)
    assert ladder_of(held)[0] == 23450 and "10:00 close 23,559.0" in held._last_reason

    await market.candle(10, 0, 23561.0)  # 101 points: all ten legs close, the ladder reopens put-heavy
    shifted = await market.check(at(10, 15, 20), 23561.0, 1.08)
    [trade] = await market.trades()
    assert trade.exit_reason == "rollover" and len(trade.legs) == 10 and {leg["side"] for leg in trade.legs} == {"short"}
    assert ladder_of(shifted) == (23600, [23400, 23500, 23600, 23700, 23800], {130.0})
    assert "shifted to 23,400-23,800" in shifted._last_reason


async def test_a_ladder_strike_without_live_prices_holds_off_the_whole_ladder(market):
    market.missing = {(23650.0, "CE")}
    skipped = await market.check(at(9, 45, 5), 23460.0, 0.92)
    assert skipped.state.get("position") is None and "no live CE and PE price at 23,650" in skipped._last_reason
    market.missing = set()
    assert ladder_of(await market.check(at(9, 45, 15), 23460.0, 0.92))[0] == 23450


async def test_everything_closes_at_1500_as_one_trade(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    closed = await market.check(at(15, 0, 3), 23470.0, 0.92)
    [trade] = await market.trades()
    assert closed.state["position"] is None and trade.exit_reason == "time_cutoff_3pm" and len(trade.legs) == 10
    after = await market.check(at(15, 0, 13) + timedelta(seconds=1), 23470.0, 0.92)
    assert after.state["position"] is None and "outside entry window" in after._last_reason
