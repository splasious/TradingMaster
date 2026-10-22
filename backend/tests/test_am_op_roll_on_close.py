"""AM OP TRD 15 MIN and AM OP TRAD 15 MIN H roll on a completed 15-minute
close (alembic d4f6a8b0c2e4, 22 Oct): the position closes and reopens only
when a candle completed since it opened closes 100 points or more from the
spot recorded then -- not the moment live NIFTY is 100 points away. The
change exists only as edits to the cards' code, so it is run here on the
built-in with them (AM OP TRAD 15 MIN H) and on the built-in with the
50-point straddle's edits too (AM OP TRD 15 MIN)."""

import importlib.util
import pathlib

import pytest

from tests.test_am_op_ma_exit import Market, at

VERSIONS = pathlib.Path(__file__).parent.parent / "alembic/versions"
BUILT_IN = pathlib.Path(__file__).parent.parent / "app/services/strategy/native_strategies/nifty_pcr_multi_regime.py"


def _migration(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evaluate(with_straddle_50: bool):
    code = BUILT_IN.read_text(encoding="utf-8")
    if with_straddle_50:
        code = _migration("f7b9d1e3a5c6_am_op_straddle_50").edited(code)
    edited = _migration("d4f6a8b0c2e4_rolls_on_15min_close").edited(code)
    assert edited is not None
    namespace: dict = {}
    exec(compile(edited, "<am-op-roll-on-close>", "exec"), namespace)
    return namespace["evaluate"]


class FiftyMarket(Market):
    STRIKE_STEP = 50


@pytest.fixture(params=["AM OP TRAD 15 MIN H", "AM OP TRD 15 MIN"])
async def market(request, db_session, monkeypatch):
    from app.services.market_data.tick_engine import tick_engine

    trd = request.param == "AM OP TRD 15 MIN"
    m = await (FiftyMarket if trd else Market)(db_session, monkeypatch, evaluate=_evaluate(trd)).build()
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


async def test_a_live_move_alone_doesnt_roll_the_straddle(market):
    await market.history(23400.0, at(1, 10, 0))
    opened = await market.check(at(1, 10, 0, 5), 1.00)
    assert opened.state["position"]["regime"] == "sideways" and opened.state["position"]["entry_spot"] == 23450.0
    jumped = await market.check(at(1, 10, 7), 1.00, spot=23580.0)  # 130 points live, mid-candle
    assert jumped.state["position"]["entry_spot"] == 23450.0 and await market.trades() == []
    assert "no 15-min close since it opened" in jumped._last_reason


async def test_a_completed_close_under_100_points_away_holds(market):
    await market.history(23400.0, at(1, 10, 0))
    await market.check(at(1, 10, 0, 5), 1.00)
    await market.candles(at(1, 10, 0), at(1, 10, 0), 23521.8)  # 10:00-10:15 closed 71.8 points on
    held = await market.check(at(1, 10, 15, 40), 1.00, spot=23560.0)
    assert await market.trades() == [] and "10:15 close 23,521.8 (71.8pt from entry; rolls at 100)" in held._last_reason


async def test_a_completed_close_100_points_away_rolls_it(market):
    await market.history(23400.0, at(1, 10, 0))
    await market.check(at(1, 10, 0, 5), 1.00)
    await market.candles(at(1, 10, 0), at(1, 10, 0), 23551.0)  # 101 points on
    rolled = await market.check(at(1, 10, 15, 40), 1.00, spot=23555.0)
    [trade] = await market.trades()
    assert trade.exit_reason == "rollover" and rolled.state["position"]["regime"] == "sideways"
    assert rolled.state["position"]["entry_spot"] == 23555.0


def test_the_edits_fit_the_built_in_once_each_and_not_twice():
    migration = _migration("d4f6a8b0c2e4_rolls_on_15min_close")
    code = BUILT_IN.read_text(encoding="utf-8")
    assert all(code.count(old) == 1 for old, _ in migration.EDITS)
    edited = migration.edited(code)
    assert edited is not None and migration.edited(edited) is None  # already rolls on a close: left alone
