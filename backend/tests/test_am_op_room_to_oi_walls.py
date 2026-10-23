"""AM OP TRD 15 MIN's room to the OI walls (alembic d1f3a5c7e9b2, 23 Oct): the
bull put and bear call spreads open -- a fresh entry or a roll's reopen --
only with live NIFTY 200 points from each of the 3 biggest-OI strikes of
their side's option type (bearish: puts; bullish: calls), either side of
NIFTY, in the PCR's 15-minute record; the straddle is unchanged. The change
exists only as edits to the card's code, so it is run here on the built-in
with the 50-point straddle's edits and these."""

import importlib.util
import pathlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from app.models.pcr import SOURCE_LIVE, PcrSnapshot, PcrStrikeOi
from tests.test_am_op_ma_exit import EXPIRY, SPOT, Market, at

VERSIONS = pathlib.Path(__file__).parent.parent / "alembic/versions"
BUILT_IN = pathlib.Path(__file__).parent.parent / "app/services/strategy/native_strategies/nifty_pcr_multi_regime.py"
# The biggest open interest by strike in the 15-minute records: well clear of NIFTY (23,450) both ways.
WALLS = {"PE": {23000.0: 3e6, 22900.0: 2e6, 22800.0: 1e6}, "CE": {24000.0: 3e6, 24100.0: 2e6, 24200.0: 1e6}}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _running_code() -> str:
    """What the card runs, as near as the repo has it: the built-in with the 50-point straddle's edits."""
    return _load("f7b9d1e3a5c6_am_op_straddle_50").edited(BUILT_IN.read_text(encoding="utf-8"))


def _edited_evaluate():
    namespace: dict = {}
    exec(compile(_load("d1f3a5c7e9b2_am_op_room_to_oi_walls").edited(_running_code()), "<am-op-room>", "exec"), namespace)
    return namespace["evaluate"]


class WallMarket(Market):
    STRIKE_STEP = 50

    def __init__(self, db, monkeypatch, evaluate=None):
        super().__init__(db, monkeypatch, evaluate)
        self.walls: dict | None = WALLS  # None: no OI records at all
        self._marks: set[datetime] = set()

    async def set_walls(self, walls: dict | None) -> None:
        """The OI from the next check's record on (the records so far dropped)."""
        await self.db.execute(delete(PcrStrikeOi))
        await self.db.execute(delete(PcrSnapshot))
        await self.db.commit()
        self.walls, self._marks = walls, set()

    async def check(self, now: datetime, pcr: float | None, spot: float = SPOT):
        mark = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        if self.walls is not None and mark not in self._marks:
            self._marks.add(mark)
            rows = [(float(k), t, self.walls.get(t, {}).get(float(k), 1000.0)) for k in range(22800, 24201, 50) for t in ("PE", "CE")]
            snap = PcrSnapshot(underlying="NIFTY", ts=mark.astimezone(timezone.utc), session_date=mark.date(), captured_at=mark,
                               source=SOURCE_LIVE, strike_window=40, expiries=[EXPIRY.isoformat()], spot=SPOT,
                               contracts_expected=len(rows), contracts_with_oi=len(rows), pcr=1.0)
            self.db.add(snap)
            await self.db.flush()
            self.db.add_all([PcrStrikeOi(snapshot_id=snap.id, expiry=EXPIRY, strike=k, option_type=t, tradingsymbol=f"N{k:.0f}{t}", oi=oi)
                             for k, t, oi in rows])
            await self.db.commit()
        return await super().check(now, pcr, spot)


@pytest.fixture
async def market(db_session, monkeypatch):
    from app.services.market_data.tick_engine import tick_engine

    m = await WallMarket(db_session, monkeypatch, evaluate=_edited_evaluate()).build()
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def _strikes(ctx) -> dict[str, float]:
    return {name: leg["strike"] for name, leg in ctx.state["position"]["legs"].items()}


def test_the_edits_fit_the_running_code_once_and_never_twice():
    migration = _load("d1f3a5c7e9b2_am_op_room_to_oi_walls")
    code = _running_code()
    assert [code.count(old) for old, _ in migration.EDITS] == [1, 1, 1]
    after = migration.edited(code)
    assert after is not None and "async def _oi_walls(" in after and migration.edited(after) is None
    assert migration.edited(code.replace("ROLL_TRIGGER = 100", "WALL_ROOM = 150\nROLL_TRIGGER = 100")) is None  # a name it adds is taken
    assert migration.edited(code.replace("    atm = round_to_nearest_100(spot)\n", "    atm = round_to_nearest_100(spot)\n" * 2)) is None  # 2 places


async def test_a_bear_call_spread_waits_while_a_broken_put_wall_is_within_200(market):
    await market.history(23500.0, at(1, 10, 0))  # MA 23500: NIFTY 23450 under it
    await market.set_walls({**WALLS, "PE": {23500.0: 3e6, 22900.0: 2e6, 22800.0: 1e6}})  # the biggest put OI 50 above NIFTY
    waiting = await market.check(at(1, 10, 0, 5), 0.70)
    assert waiting.state.get("position") is None and waiting._last_action == "skipped"
    assert ("PCR 0.700: bearish spread waits -- put OI wall 23,500 is 50 pts above NIFTY 23,450.0 (needs 200 from each of the "
            "top 3 put OI strikes: 22,800, 22,900, 23,500)") in waiting._last_reason

    await market.set_walls({**WALLS, "PE": {23000.0: 4e6, 22900.0: 3e6, 22800.0: 2e6, 23500.0: 1e6}})  # 23,500 pushed to 4th
    entered = await market.check(at(1, 10, 15, 5), 0.70)
    assert entered.state["position"]["regime"] == "bearish" and _strikes(entered) == {"short_ce": 23700.0, "long_ce": 23900.0}


async def test_a_bull_put_spread_waits_while_a_call_wall_is_within_200(market):
    await market.history(23400.0, at(1, 10, 0))
    await market.set_walls({**WALLS, "CE": {24000.0: 3e6, 24100.0: 2e6, 23600.0: 1e6}})  # the 3rd biggest, 150 above
    waiting = await market.check(at(1, 10, 0, 5), 1.30)
    assert waiting.state.get("position") is None and "bullish spread waits -- call OI wall 23,600 is 150 pts above NIFTY" in waiting._last_reason
    opened = await market.check(at(1, 10, 1), 1.30, spot=23399.0)  # NIFTY 201 under it: room
    assert opened.state["position"]["regime"] == "bullish"


async def test_the_straddle_never_waits_for_the_walls(market):
    await market.history(23400.0, at(1, 10, 0))
    await market.set_walls({"PE": {23400.0: 9e6}, "CE": {23500.0: 9e6}})  # walls right at NIFTY
    ctx = await market.check(at(1, 10, 0, 5), 1.00)
    assert ctx.state["position"]["regime"] == "sideways"


async def test_no_oi_record_holds_off_the_spreads_only(market):
    await market.history(23400.0, at(1, 10, 0))
    await market.set_walls(None)
    none = await market.check(at(1, 10, 0, 5), 1.30)
    assert none.state.get("position") is None and "PCR 1.300: bullish spread waits -- no 15-min OI record" in none._last_reason
    assert (await market.check(at(1, 10, 1), 1.00)).state["position"]["regime"] == "sideways"


async def test_a_roll_whose_reopen_is_blocked_closes_the_spread_and_waits(market):
    await market.history(23400.0, at(1, 10, 0))
    opened = await market.check(at(1, 10, 0, 5), 1.30)  # bull put spread at 23,450
    assert opened.state["position"]["regime"] == "bullish"

    await market.set_walls({**WALLS, "CE": {23700.0: 3e6, 24100.0: 2e6, 24200.0: 1e6}})  # a call wall 140 above the new NIFTY
    rolled = await market.check(at(1, 10, 15, 5), 1.30, spot=23560.0)  # 110 points: the roll
    [trade] = await market.trades()
    assert trade.exit_reason == "rollover" and rolled.state.get("position") is None
    assert rolled._last_action == "exited" and rolled._last_signal == "COVER"
    assert "bullish spread rolled out, the reopen waits -- call OI wall 23,700 is 140 pts above NIFTY 23,560.0" in rolled._last_reason

    await market.set_walls(WALLS)  # the wall gone from the next record: a fresh bull put spread
    back = await market.check(at(1, 10, 30, 5), 1.30, spot=23560.0)
    assert back.state["position"]["regime"] == "bullish" and back._last_action == "entered"


async def test_an_open_spread_is_never_closed_by_the_rule(market):
    await market.history(23400.0, at(1, 10, 0))
    await market.check(at(1, 10, 0, 5), 1.30)
    await market.set_walls({**WALLS, "CE": {23500.0: 9e6}})  # a call wall 50 above, but under 100 points moved: held
    held = await market.check(at(1, 10, 15, 5), 1.30, spot=23480.0)
    assert held.state["position"] is not None and held._last_action == "hold" and await market.trades() == []
