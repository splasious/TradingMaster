"""AM OP TRD 15 MIN's straddle strike (alembic f7b9d1e3a5c6, 16 Oct): of the
two 50-point strikes either side of NIFTY, the one with the higher CE + PE
premium; the spreads keep their 100-point strikes. The change exists only
as edits to the card's code, so it is run here on the built-in with them."""

import importlib.util
import pathlib

import pytest

from tests.test_am_op_ma_exit import Market, at

VERSIONS = pathlib.Path(__file__).parent.parent / "alembic/versions"
BUILT_IN = pathlib.Path(__file__).parent.parent / "app/services/strategy/native_strategies/nifty_pcr_multi_regime.py"


def _edited_evaluate():
    spec = importlib.util.spec_from_file_location("straddle_50", VERSIONS / "f7b9d1e3a5c6_am_op_straddle_50.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    namespace: dict = {}
    exec(compile(migration.edited(BUILT_IN.read_text(encoding="utf-8")), "<am-op-straddle-50>", "exec"), namespace)
    return namespace["evaluate"]


class FiftyMarket(Market):
    STRIKE_STEP = 50


@pytest.fixture
async def market(db_session, monkeypatch):
    from app.services.market_data.tick_engine import tick_engine

    m = await FiftyMarket(db_session, monkeypatch, evaluate=_edited_evaluate()).build()
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def _strikes(ctx) -> dict[str, float]:
    return {name: leg["strike"] for name, leg in ctx.state["position"]["legs"].items()}


async def test_the_straddle_takes_the_50_point_strike_with_the_higher_premium_sum(market):
    await market.history(23400.0, at(1, 10, 0))
    market.premiums = {(23450.0, "CE"): 120.0, (23450.0, "PE"): 110.0, (23500.0, "CE"): 95.0, (23500.0, "PE"): 150.0}
    ctx = await market.check(at(1, 10, 0, 5), 1.00, spot=23460.0)  # 23,450: 230 vs 23,500: 245
    assert ctx.state["position"]["regime"] == "sideways" and _strikes(ctx) == {"short_ce": 23500.0, "short_pe": 23500.0}


async def test_the_nearer_strike_on_a_tie(market):
    await market.history(23400.0, at(1, 10, 0))
    tie = await market.check(at(1, 10, 0, 5), 1.00, spot=23460.0)  # every premium 100: the nearer, 23,450
    assert _strikes(tie) == {"short_ce": 23450.0, "short_pe": 23450.0}


async def test_the_spreads_keep_their_100_point_strikes(market):
    await market.history(23400.0, at(1, 10, 0))
    ctx = await market.check(at(1, 10, 0, 5), 1.30, spot=23460.0)  # ATM 23,500 on 100s
    assert _strikes(ctx) == {"short_pe": 23300.0, "long_pe": 23100.0}
