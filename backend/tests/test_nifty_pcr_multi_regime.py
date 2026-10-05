import pytest

from app.services.strategy.native_strategies.nifty_pcr_multi_regime import (
    determine_regime,
    leg_specs_for,
    pcr_exit_band,
    round_to_nearest_100,
)


def test_determine_regime_with_hysteresis_buffers():
    assert determine_regime(1.0) == "sideways"
    assert determine_regime(0.80) == "sideways"
    assert determine_regime(1.20) == "sideways"
    assert determine_regime(1.30) == "bullish"
    assert determine_regime(0.70) == "bearish"
    assert determine_regime(0.78) == "buffer"
    assert determine_regime(1.22) == "buffer"


def test_pcr_exit_band_matches_each_regimes_exit_rule():
    assert pcr_exit_band("sideways") == (0.75, 1.25)  # exit if PCR > 1.25 or < 0.75
    assert pcr_exit_band("bullish") == (1.20, None)  # exit when PCR falls below 1.20
    assert pcr_exit_band("bearish") == (None, 0.80)  # exit when PCR rises above 0.80
    with pytest.raises(ValueError):
        pcr_exit_band("buffer")


def test_leg_specs_and_rounding():
    assert round_to_nearest_100(23450) == 23500  # ties round up, never banker's rounding
    assert round_to_nearest_100(23449) == 23400
    assert leg_specs_for("sideways", 23400) == {"short_ce": (23400, "CE", "sell"), "short_pe": (23400, "PE", "sell")}
    assert leg_specs_for("bullish", 23400) == {"short_pe": (23200, "PE", "sell"), "long_pe": (23000, "PE", "buy")}
    assert leg_specs_for("bearish", 23400) == {"short_ce": (23600, "CE", "sell"), "long_ce": (23800, "CE", "buy")}


# --- Rolls on completed 15-minute closes only (agreed 5 Oct) ---------------

import uuid  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402

from sqlalchemy import select  # noqa: E402

import app.services.strategy.native_strategies.nifty_pcr_multi_regime as am  # noqa: E402
from app.models.instrument import Instrument  # noqa: E402
from app.services.broker.zerodha_broker import IST  # noqa: E402


def ist(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 5, hh, mm, ss, tzinfo=IST)


class FakeCtx:
    """Just the NativeContext calls AM OP makes; NIFTY at `spot`, PCR sideways."""

    def __init__(self, db, state: dict, now: datetime, spot: float):
        self.db, self.state, self.now, self.spot = db, state, now, spot
        self.opened: list[tuple] = []
        self.closed: list[tuple] = []
        self.trades: list[dict] = []
        self.woke: datetime | None = None
        self.last: tuple | None = None

    async def get_price(self, instrument_id):
        nifty = (await self.db.execute(select(Instrument).where(Instrument.symbol == "NIFTY 50"))).scalar_one()
        return self.spot if instrument_id == nifty.id else 100.0

    async def get_pcr(self, timeframe="15m"):
        return 1.0

    async def list_weekly_expiries(self, underlying_id, today):
        return [date(2026, 10, 6), date(2026, 10, 13)]

    async def find_option(self, underlying_id, expiry, strike, option_type):
        symbol = f"NIFTY{expiry:%y%m%d}{int(strike)}{option_type}"
        found = (await self.db.execute(select(Instrument).where(Instrument.symbol == symbol))).scalar_one_or_none()
        if found is None:
            found = Instrument(exchange="NFO", symbol=symbol, name=symbol, instrument_type="option", data_source="test",
                               external_ref=symbol, expiry=expiry, strike=float(strike), option_type=option_type, lot_size=65)
            self.db.add(found)
            await self.db.flush()
        return found

    async def open_leg(self, instrument, side, quantity, price):
        self.opened.append((instrument.symbol, side))

    async def close_leg(self, instrument, side, quantity, price):
        self.closed.append((instrument.symbol, side))

    async def record_trade(self, **trade):
        self.trades.append(trade)

    def note(self, action, signal=None, reason=None):
        self.last = (action, signal, reason)

    def wake_at(self, when):
        self.woke = when


class Day:
    def __init__(self, db):
        self.db, self.state, self.trades = db, {}, []

    async def check(self, now: datetime, spot: float) -> FakeCtx:
        if (await self.db.execute(select(Instrument).where(Instrument.symbol == "NIFTY 50"))).scalar_one_or_none() is None:
            self.db.add(Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index",
                                   data_source="test", external_ref="NIFTY 50"))
            await self.db.flush()
        ctx = FakeCtx(self.db, self.state, now, spot)
        await am.evaluate(ctx)
        self.trades += ctx.trades
        return ctx


def test_completed_and_next_close():
    assert am.completed_close(ist(9, 59, 59)) is None  # before the first roll close
    assert am.completed_close(ist(10, 26, 9)) == ist(10, 15)
    assert am.completed_close(ist(14, 59)) == ist(14, 45)
    assert am.completed_close(ist(15, 0, 2)) is None  # 15:00 is the hard exit, not a roll
    assert am.next_close(ist(10, 26, 9)) == ist(10, 30)
    assert am.next_close(ist(10, 30, 0)) == ist(10, 45)
    assert am.next_close(ist(14, 50)) == ist(15, 0) and am.next_close(ist(15, 1)) is None


async def test_5_oct_no_roll_mid_candle_rolls_on_the_first_close_100_away(db_session):
    day = Day(db_session)
    ctx = await day.check(ist(9, 45, 0), 22583.0)
    assert sorted(ctx.opened) == [("NIFTY26100622600CE", "sell"), ("NIFTY26100622600PE", "sell")]
    assert ctx.woke == ist(10, 0) + am.CLOSE_WAKE and day.state["position"]["roll_checked_close"] is None

    for at, spot in ((ist(10, 0, 2), 22567.3), (ist(10, 15, 2), 22543.75)):
        assert (await day.check(at, spot)).last[0] == "hold"
    ctx = await day.check(ist(10, 26, 9), 22480.4)  # 102.6 points -- the old rule rolled here
    assert ctx.last[0] == "hold" and not ctx.closed and "rolls on a 15-min close 100pt away" in ctx.last[2]
    ctx = await day.check(ist(10, 30, 2), 22516.15)  # the close: 66.9 points
    assert not ctx.closed and day.state["position"]["last_close_at"] == "10:30"

    ctx = await day.check(ist(10, 45, 2), 22480.0)  # the close: 103 points -> roll at the new ATM
    assert sorted(ctx.closed) == [("NIFTY26100622600CE", "buy"), ("NIFTY26100622600PE", "buy")]
    assert sorted(ctx.opened) == [("NIFTY26100622500CE", "sell"), ("NIFTY26100622500PE", "sell")]
    assert [t["exit_reason"] for t in day.trades] == ["rollover"]
    position = day.state["position"]
    assert position["entry_spot"] == 22480.0 and position["roll_checked_close"] == ist(10, 45).isoformat()

    ctx = await day.check(ist(10, 45, 12), 22370.0)  # 110 more, same candle: that close is done
    assert not ctx.closed
    ctx = await day.check(ist(11, 0, 2), 22370.0)  # the next close
    assert len(day.trades) == 2 and sorted(ctx.opened) == [("NIFTY26100622400CE", "sell"), ("NIFTY26100622400PE", "sell")]


async def test_a_close_checked_late_is_skipped_not_rolled_on(db_session):
    day = Day(db_session)
    await day.check(ist(9, 45, 0), 22583.0)
    ctx = await day.check(ist(10, 2, 30), 22400.0)  # first check 2.5 min after the 10:00 close
    assert not ctx.closed and "the 10:00 close was missed" in ctx.last[2]
    ctx = await day.check(ist(10, 15, 2), 22400.0)  # the next close is checked as usual
    assert len(day.trades) == 1


async def test_a_position_from_the_old_version_is_checked_from_the_next_close(db_session):
    day = Day(db_session)
    await day.check(ist(9, 45, 0), 22583.0)
    del day.state["position"]["roll_checked_close"]  # as an older version saved it
    ctx = await day.check(ist(10, 26, 9), 22480.0)
    assert not ctx.closed and day.state["position"]["roll_checked_close"] == ist(10, 15).isoformat()
    ctx = await day.check(ist(10, 30, 2), 22480.0)
    assert len(day.trades) == 1
