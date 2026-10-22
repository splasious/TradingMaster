"""Conti Ladder Straddle 15 MIN (native_strategies/nifty_conti_ladder.py, 22
Oct): five NIFTY short straddles, 2 lots a leg, at the PCR-picked middle
strike and 100 and 200 points either side, held from 09:45 to 15:00. Since
version 2 the ladder steps with live NIFTY, 100 points at a time, trading
only the straddle that drops off the far end and the one that opens beyond
the near end."""

from datetime import timezone

import pytest

import app.services.strategy.native_strategies.nifty_conti_ladder as ladder
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


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await Ladder(db_session).build(monkeypatch)
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def ladder_of(ctx) -> tuple[float, list[float], set[float]]:
    p = ctx.state["position"]
    return p["strike"], p["strikes"], {leg["quantity"] for leg in p["legs"].values()}


def strikes_traded(trade) -> set[float]:
    return {leg["strike"] for leg in trade.legs}


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
    assert opened.state["position"]["reference"] == 23460.0
    assert opened._last_signal == "STRADDLE" and "opened at 23,250-23,650" in opened._last_reason


async def test_a_100_point_live_move_steps_only_the_far_straddle(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    held = await market.check(at(10, 5), 23559.0, 0.92)  # 99 points: nothing moves
    assert ladder_of(held)[1] == [23250, 23350, 23450, 23550, 23650] and await market.trades() == []
    assert "steps at 23,360.0 / 23,560.0" in held._last_reason

    stepped = await market.check(at(10, 6), 23561.0, 1.30)  # 101 points, mid-candle: one step up
    [trade] = await market.trades()
    assert trade.exit_reason == "rollover" and strikes_traded(trade) == {23250} and len(trade.legs) == 2
    assert ladder_of(stepped) == (23550, [23350, 23450, 23550, 23650, 23750], {130.0})
    p = stepped.state["position"]
    assert p["reference"] == 23560.0 and p["side"] == "call"  # the PCR isn't read again
    assert p["legs"]["short_23750_ce"]["opened_at"] == at(10, 6).astimezone(timezone.utc).isoformat()
    assert p["legs"]["short_23350_ce"]["opened_at"] == at(9, 45, 5).astimezone(timezone.utc).isoformat()  # kept as they were
    assert "23,250 closed / 23,750 opened" in stepped._last_reason

    down = await market.check(at(10, 40), 23459.0, 0.92)  # back 101 from the new reference: one step down
    assert ladder_of(down)[1] == [23250, 23350, 23450, 23550, 23650]
    assert [strikes_traded(t) for t in await market.trades()] == [{23250}, {23750}]


async def test_a_230_point_jump_steps_twice_in_one_check(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    jumped = await market.check(at(10, 5), 23690.0, 0.92)
    assert ladder_of(jumped) == (23650, [23450, 23550, 23650, 23750, 23850], {130.0})
    assert [strikes_traded(t) for t in await market.trades()] == [{23250}, {23350}]
    assert jumped.state["position"]["reference"] == 23660.0


async def test_no_live_price_at_the_new_strike_holds_off_the_step(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    market.missing = {(23750.0, "PE")}
    waiting = await market.check(at(10, 5), 23565.0, 0.92)
    assert ladder_of(waiting)[1] == [23250, 23350, 23450, 23550, 23650] and await market.trades() == []
    assert "no live CE and PE price at 23,750" in waiting._last_reason
    market.missing = set()
    assert ladder_of(await market.check(at(10, 5, 10), 23565.0, 0.92))[1] == [23350, 23450, 23550, 23650, 23750]


async def test_everything_closes_at_1500_one_trade_per_straddle(market):
    await market.check(at(9, 45, 5), 23460.0, 0.92)
    closed = await market.check(at(15, 0, 3), 23470.0, 0.92)
    trades = await market.trades()
    assert closed.state["position"] is None and len(trades) == 5
    assert {t.exit_reason for t in trades} == {"time_cutoff_3pm"} and all(len(t.legs) == 2 for t in trades)
    after = await market.check(at(15, 0, 13), 23470.0, 0.92)
    assert after.state["position"] is None and "outside entry window" in after._last_reason


async def test_a_ladder_opened_by_version_1_steps_too(market):
    opened = await market.check(at(9, 45, 5), 23460.0, 0.92)
    p = opened.state["position"]
    p.pop("reference")  # version 1's position: no reference, no opening time on its legs
    for leg in p["legs"].values():
        leg.pop("opened_at")
    market.deployment.state = {"position": p}
    await market.db.commit()
    stepped = await market.check(at(10, 6), 23561.0, 0.92)
    assert ladder_of(stepped)[1] == [23350, 23450, 23550, 23650, 23750] and stepped.state["position"]["reference"] == 23560.0
    [trade] = await market.trades()
    assert trade.opened_at.replace(tzinfo=timezone.utc) == at(9, 45, 5).astimezone(timezone.utc)
