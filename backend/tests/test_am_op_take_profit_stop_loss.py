"""AM OP TRD 15 MIN's take-profit and stop-loss (alembic e2a4c6e8f0b3, 9 Nov):
each open position's P&L at its legs' last prices, before charges, since it
opened or was last rolled -- at +Rs 14,000 all its legs close and the next
opens at the next check; at -Rs 10,000 they close and none opens again that
day. The change exists only as edits to the card's code, so it is run here
on the built-in with the 50-point straddle's and the OI walls' edits, and
these."""

import importlib.util
import pathlib
import uuid

import pytest

from tests.test_am_op_ma_exit import Market, at
from tests.test_am_op_room_to_oi_walls import WALLS, WallMarket

VERSIONS = pathlib.Path(__file__).parent.parent / "alembic/versions"
BUILT_IN = pathlib.Path(__file__).parent.parent / "app/services/strategy/native_strategies/nifty_pcr_multi_regime.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _running_code() -> str:
    """What the card runs, as near as the repo has it: the built-in with the
    50-point straddle's edits and the OI walls'."""
    code = _load("f7b9d1e3a5c6_am_op_straddle_50").edited(BUILT_IN.read_text(encoding="utf-8"))
    return _load("d1f3a5c7e9b2_am_op_room_to_oi_walls").edited(code)


def _edited_evaluate():
    namespace: dict = {}
    exec(compile(_load("e2a4c6e8f0b3_am_op_take_profit_stop_loss").edited(_running_code()), "<am-op-tp-sl>", "exec"), namespace)
    return namespace["evaluate"]


@pytest.fixture
async def market(db_session, monkeypatch):
    from app.services.market_data.tick_engine import tick_engine

    m = await WallMarket(db_session, monkeypatch, evaluate=_edited_evaluate()).build()
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def _qty(ctx) -> float:
    return next(iter(ctx.state["position"]["legs"].values()))["quantity"]


def test_the_edits_fit_the_running_code_once_and_never_twice():
    migration = _load("e2a4c6e8f0b3_am_op_take_profit_stop_loss")
    code = _running_code()
    assert [code.count(old) for old, _ in migration.EDITS] == [1, 1, 1, 1]
    after = migration.edited(code)
    assert after is not None and "async def _open_pnl(" in after and migration.edited(after) is None
    assert migration.edited(code.replace("ROLL_TRIGGER = 100", "STOP_LOSS = 5\nROLL_TRIGGER = 100")) is None  # a name it adds is taken
    assert migration.edited(code.replace("import uuid\n", "")) is None  # it needs uuid imported
    no_window = code.replace("    if not in_entry_window(now_ist.time()):\n", "    if not (in_entry_window(now_ist.time())):\n")
    assert migration.edited(no_window) is None  # a required place gone
    docstring_gone = code.replace("Entry: 9:45 AM IST onward.", "Entry: from 9:45.")
    assert migration.edited(docstring_gone) is not None  # the docstring's note is optional
    assert "TAKE_PROFIT = 14_000.0" in after and "STOP_LOSS = 10_000.0" in after


async def test_the_straddle_closes_at_14000_profit_and_the_next_opens_at_the_next_check(market):
    await market.history(23400.0, at(1, 10, 0))
    opened = await market.check(at(1, 10, 0, 5), 1.00)
    assert opened.state["position"]["regime"] == "sideways"
    qty = _qty(opened)
    legs = opened.state["position"]["legs"]
    strikes = {leg["strike"] for leg in legs.values()}
    short = 14_000.0 / (2 * qty)  # each sold leg's fall in premium that makes Rs 14,000
    for k in strikes:
        market.premiums[(k, "CE")] = market.premiums[(k, "PE")] = 100.0 - short + 0.05  # just short: held
    held = await market.check(at(1, 10, 5), 1.00)
    assert held.state["position"] is not None and held._last_action == "hold"

    for k in strikes:
        market.premiums[(k, "CE")] = market.premiums[(k, "PE")] = 100.0 - short  # Rs 14,000 exactly
    hit = await market.check(at(1, 10, 6), 1.00)
    assert hit.state.get("position") is None and hit._last_signal == "COVER"
    assert hit._last_reason.startswith("sideways position closed at take-profit: P&L +14,000 at last prices")
    assert "no new position" not in hit._last_reason and "_pnl_stop" not in hit.state
    [trade] = await market.trades()
    assert trade.exit_reason == "take_profit" and trade.pnl == pytest.approx(14_000.0)

    again = await market.check(at(1, 10, 6, 10), 1.00)  # the next check: a new straddle
    assert again.state["position"]["regime"] == "sideways"


async def test_a_10000_loss_closes_it_and_no_new_position_opens_until_the_next_day(market):
    await market.history(23400.0, at(1, 10, 0))
    opened = await market.check(at(1, 10, 0, 5), 1.30, spot=23399.0)  # a bull put spread
    assert opened.state["position"]["regime"] == "bullish"
    qty = _qty(opened)
    legs = opened.state["position"]["legs"]
    sold = next(leg for leg in legs.values() if leg["side"] == "sell")
    market.premiums[(sold["strike"], "PE")] = 100.0 + 10_000.0 / qty  # the sold put up Rs 10,000 worth; the bought one flat
    hit = await market.check(at(1, 10, 5), 1.30, spot=23399.0)
    assert hit.state.get("position") is None
    assert hit._last_reason.startswith("bullish position closed at stop-loss: P&L -10,000 at last prices")
    assert hit._last_reason.endswith("; no new position today") and "_pnl_stop" in hit.state
    [trade] = await market.trades()
    assert trade.exit_reason == "stop_loss" and trade.pnl == pytest.approx(-10_000.0)

    market.premiums.clear()
    for later in (at(1, 10, 5, 10), at(1, 13, 0)):
        ctx = await market.check(later, 1.30, spot=23399.0)
        assert ctx.state.get("position") is None and ctx._last_action == "skipped", later
        assert ctx._last_reason.startswith("stop-loss hit at 10:05:00: no new position until the next trading day")
    await market.candles(at(1, 10, 0), at(1, 15, 15), 23400.0)
    await market.candles(at(5, 9, 15), at(5, 9, 45), 23400.0)
    nxt = await market.check(at(5, 10, 0, 5), 1.30, spot=23399.0)  # the next trading day (2 Oct is a holiday)
    assert nxt.state["position"]["regime"] == "bullish" and "_pnl_stop" not in nxt.state


async def test_a_leg_with_no_price_means_no_reading(market):
    from app.services.market_data.tick_engine import tick_engine

    await market.history(23400.0, at(1, 10, 0))
    opened = await market.check(at(1, 10, 0, 5), 1.00)
    qty = _qty(opened)
    strikes = {leg["strike"] for leg in opened.state["position"]["legs"].values()}
    for k in strikes:
        market.premiums[(k, "CE")] = market.premiums[(k, "PE")] = 100.0 + 20_000.0 / qty  # a big loss on paper...
    original = market.prices

    def prices_without_one(now, spot=23450.0):
        original(now, spot)
        leg = next(iter(opened.state["position"]["legs"].values()))
        tick_engine.forget([uuid.UUID(leg["instrument_id"])])  # no tick, and no candle to fall back on

    market.prices = prices_without_one
    ctx = await market.check(at(1, 10, 5), 1.00)  # ...but one leg unpriced: no reading, nothing closed on it
    assert ctx.state["position"] is not None and ctx._last_action == "hold"
