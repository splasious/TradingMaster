"""OI Change Buy 15 MIN (native_strategies/nifty_oi_change_buy.py, 16 Nov).
Since version 8 (26 Nov) the side follows the live candle -- the PCR tab's
LIVE row, its Put / Call dOI since the latest record -- to 15:29, and its
trades are charged no brokerage; since version 9 (27 Nov) from the 09:15
open, where the 09:00 row's own (overnight) change decides first. Before:
at every 15-minute close 09:30-15:15 the PCR records' change in OI over the
last 2 fifteen-minute candles, averaged (version 6, 24 Nov; that candle alone
in versions 2-5, the day's in version 1) -- put side higher, one lot of the
ATM NIFTY call bought; call side higher, the ATM put -- on the nearest
weekly (next week's on its expiry day) at the ask; the side flipping, the
held option sold at the bid and the other bought 5 seconds later; the
+15% / 5% trailing stop. No stop-loss and no 09:15:50 first-minute buy since
version 6; since version 7 (25 Nov) nothing is sold at 15:29 -- what's held is
carried overnight, and rolled to next week's weekly at 12:00 on its expiry
day."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import app.services.strategy.native_strategies.nifty_oi_change_buy as oib
from app.models.paper_trading import PaperNativeTrade
from app.models.pcr import SOURCE_LIVE, PcrSnapshot
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from tests.test_conti_calendar import SPREAD, W1, W2, Market, at


class OIMarket(Market):
    live: tuple[datetime, float | None, float | None] | None = None  # version 8: what the PCR tab's LIVE row reads

    async def live_row(self, now_utc: datetime):
        """The fake LIVE row: the latest `live` reading -- its Put / Call dOI since its record."""
        if self.live is None:
            return None, "Zerodha isn't logged in"
        since, put, call = self.live
        return {"ts": now_utc, "prev_ts": since.astimezone(timezone.utc), "put_oi_change": put, "call_oi_change": call}, None

    def reads(self, since: datetime, put: float | None, call: float | None) -> None:
        """Version 8: the live candle since the record at `since` reads `put` / `call` from now on."""
        self.live = (since, put, call)

    async def record(self, mark: datetime, put_day: float | None, call_day: float | None) -> None:
        """The PCR Analysis tab's record for the close `mark`, a few seconds
        after it: `put_day` / `call_day` its last 15 minutes' change in OI
        (version 2) -- the day's change the other way round, so a rule on the
        day's would pick the other side."""
        self.db.add(PcrSnapshot(
            underlying="NIFTY", ts=mark.astimezone(timezone.utc), session_date=mark.date(),
            captured_at=(mark + timedelta(seconds=4)).astimezone(timezone.utc), source=SOURCE_LIVE, strike_window=40,
            expiries=[], put_oi_change=put_day, call_oi_change=call_day, put_oi_change_day=call_day, call_oi_change_day=put_day,
        ))
        await self.db.commit()
        self.reads(mark, put_day, call_day)  # version 8: the live candle from this close reads the same, for these tests

    async def check(self, now: datetime, spot: float = 23460.0) -> NativeContext:
        self.prices(now, spot)
        ctx = NativeContext(db=self.db, portfolio=self.portfolio, deployment=self.deployment, state=self.deployment.state or {},
                            now=now.astimezone(timezone.utc))
        await oib.evaluate(ctx)
        self.deployment.state = ctx.state
        await self.db.commit()
        return ctx

    def ltp(self, expiry, strike: float, option_type: str, now: datetime, spot: float) -> float:
        return tick_engine.get_current_price(self.option(expiry, strike, option_type).id)


@pytest.fixture
async def market(db_session, monkeypatch):
    m = await OIMarket(db_session).build(monkeypatch)
    monkeypatch.setattr(oib, "live_row", m.live_row)
    m.deployment.state = {"last_mark": at(4, 9, 15).isoformat()}  # a card already running before 09:30
    yield m
    tick_engine.forget([m.nifty.id, *(o.id for o in m.options)])


def test_the_side_and_the_strike():  # unwinding (a negative change) compared as it is
    assert oib.wanted_side(5_00_000, 2_00_000) == "CE" and oib.wanted_side(-1_00_000, 3_00_000) == "PE"
    assert oib.wanted_side(1.0, 1.0) is None and oib.wanted_side(None, 1.0) is None
    assert oib.wanted_side(-1_00_000, -4_00_000) == "CE" and oib.wanted_side(-4_00_000, -1_00_000) == "PE"  # both unwinding
    assert (oib.atm_strike(23460.0), oib.atm_strike(23474.9), oib.atm_strike(23475.0)) == (23450.0, 23450.0, 23500.0)
    assert oib.latest_mark(at(4, 9, 29)) is None and oib.latest_mark(at(4, 9, 44, 59)) == at(4, 9, 30)
    assert oib.latest_mark(at(4, 15, 28)) == at(4, 15, 15) and oib.next_mark(at(4, 15, 15)) is None
    assert oib.next_mark(at(4, 9, 0)) == at(4, 9, 30) and oib.next_mark(at(4, 9, 30)) == at(4, 9, 45)
    assert (oib.LOTS, oib.FLIP_GAP, oib.DAY_CLOSE.strftime("%H:%M")) == (1, timedelta(seconds=5), "15:29")
    assert (oib.AVERAGE_CANDLES, oib.TRAIL_START, oib.TRAIL) == (1, 0.15, 0.05)  # version 8: a candle is its own
    assert (oib.LIVE_CHECK, oib.BROKERAGE_PER_ORDER) == (timedelta(seconds=15), 0.0)
    assert not hasattr(oib, "STOP_LOSS_PER_LOT") and not hasattr(oib, "FIRST_READ") and not hasattr(oib, "RECORD_WAIT")
    assert oib.closes_a_candle(at(4, 9, 30)) and oib.closes_a_candle(at(4, 15, 30))
    assert not oib.closes_a_candle(at(4, 9, 15)) and not oib.closes_a_candle(at(4, 9, 0))  # pre-open and overnight


async def test_at_the_0915_open_the_0900_row_decides_one_lot_of_the_atm_weekly_at_the_ask(market):
    """27 Nov (version 9): the 09:00 row's own change -- the overnight -- decides at the open; then the live candle."""
    early = await market.check(at(4, 9, 14))
    assert early.state.get("position") is None and early._wake_at == at(4, 9, 15)
    await market.record(at(4, 9, 0), put_day=91_34_190, call_day=32_96_605)  # the overnight: the put side higher
    ctx = await market.check(at(4, 9, 15, 2))
    p = ctx.state["position"]
    assert (p["option"], p["option_type"], p["strike"], p["weekly_expiry"]) == ("09 Nov 23,450 CE", "CE", 23450.0, W1.isoformat())
    assert p["bias"] == "long call" and p["regime"] == "oi change buy" and p["decided_at"] == "the 09:00 row (overnight)"
    leg = p["legs"]["long"]
    assert leg["quantity"] == 65 and leg["entry_price"] == market.ltp(W1, 23450.0, "CE", at(4, 9, 15, 2), 23460.0) + SPREAD  # the ask
    assert ctx._last_signal == "BUY" and "the 09:00 row (overnight) change in OI: put +9,134,190, call +3,296,605" in ctx._last_reason
    assert ctx._wake_at == at(4, 9, 15, 3)  # version 3: read every second while held, for the trail
    assert p["trail_starts_at"] == pytest.approx(leg["entry_price"] * 1.15, abs=0.01) and p["trail_stop"] is None
    assert "stop_loss_at" not in p and "no stop-loss" in ctx._last_reason and ctx.state["opened_on"] == "2026-11-04"
    assert (p["put_change_live"], p["call_change_live"]) == (91_34_190, 32_96_605) and ctx.state["brokerage_per_order"] == 0.0
    waits = await market.check(at(4, 9, 15, 4))  # the live row still against 09:00: the 09:15 record not in
    assert waits.state["position"]["option_type"] == "CE" and "waiting for the 09:15 record" in waits._last_reason
    await market.record(at(4, 9, 15), put_day=1_00_000, call_day=4_00_000)  # in: the live candle from 09:15 reads the call side
    flipped = await market.check(at(4, 9, 15, 9))
    assert [t.exit_reason for t in await market.trades()] == ["oi_flip"] and flipped.state["pending"]["option_type"] == "PE"
    assert "live 09:15:09 (since 09:15) change in OI: put +100,000, call +400,000 -- now a put" in flipped._last_reason


async def test_the_0900_row_decides_only_once_a_day_and_without_one_the_live_candle_decides_from_0915(market):
    await market.record(at(4, 9, 0), put_day=5_00_000, call_day=1_00_000)
    await market.check(at(4, 9, 15, 2))
    await market.record(at(4, 9, 15), put_day=3_00_000, call_day=1_00_000)
    held = await market.check(at(4, 9, 20))  # the live candle: still the put side -- and the 09:00 row isn't read again
    assert held.state["position"]["option_type"] == "CE" and "live 09:20:00 (since 09:15)" in held._last_reason
    market.deployment.state = {**market.deployment.state, "position": None, "opened_on": "2026-11-03"}
    fresh = await market.check(at(5, 9, 15, 2))  # 5 Nov: no 09:00 row that day -- the live candle, once the 09:15 record is in
    assert fresh.state["opened_on"] == "2026-11-05" and fresh.state.get("position") is None
    assert "waiting for the 09:15 record" in fresh._last_reason
    await market.record(at(5, 9, 15), put_day=1_00_000, call_day=4_00_000)
    assert (await market.check(at(5, 9, 15, 8))).state["position"]["option_type"] == "PE"


async def test_the_same_side_keeps_the_option_held_and_a_flip_sells_it_then_buys_the_other_5_seconds_later(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    first = (await market.check(at(4, 9, 30, 4))).state["position"]
    await market.record(at(4, 9, 45), put_day=7_00_000, call_day=4_00_000)
    held = await market.check(at(4, 9, 45, 5), spot=23560.0)  # NIFTY has moved: the same contract held
    assert held.state["position"]["option"] == first["option"] and "still a call: held" in held._last_reason
    assert await market.trades() == []

    await market.record(at(4, 10, 0), put_day=3_00_000, call_day=9_00_000)  # the call side now higher
    sold = await market.check(at(4, 10, 0, 4), spot=23560.0)
    [trade] = await market.trades()
    assert trade.exit_reason == "oi_flip" and trade.legs[0]["exit_price"] == market.ltp(W1, 23450.0, "CE", at(4, 10, 0, 4), 23560.0) - SPREAD
    assert sold.state.get("position") is None and sold._wake_at == at(4, 10, 0, 9) and "the put bought at 10:00:09" in sold._last_reason
    between = await market.check(at(4, 10, 0, 6), spot=23560.0)
    assert between.state.get("position") is None and between._wake_at == at(4, 10, 0, 9)
    bought = await market.check(at(4, 10, 0, 9), spot=23560.0)
    p = bought.state["position"]
    assert (p["option"], p["bias"]) == ("09 Nov 23,550 PE", "long put")  # NIFTY 23,560's ATM now
    assert p["legs"]["long"]["entry_price"] == market.ltp(W1, 23550.0, "PE", at(4, 10, 0, 9), 23560.0) + SPREAD


async def test_an_equal_change_keeps_what_is_held_and_no_live_reading_keeps_it_too(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    await market.check(at(4, 9, 30, 4))
    market.reads(at(4, 9, 30), put=3_75_000, call=3_75_000)
    equal = await market.check(at(4, 9, 40))
    assert equal.state["position"]["option_type"] == "CE" and "no change" in equal._last_reason
    market.live = None  # Zerodha logged out: no live reading
    blind = await market.check(at(4, 9, 41))
    assert blind.state["position"]["option_type"] == "CE" and await market.trades() == []
    assert "no live change-in-OI reading (Zerodha isn't logged in): what's held kept" in blind._last_reason


async def test_at_1529_nothing_is_sold_it_is_carried_overnight_and_nothing_more_is_bought(market):
    """25 Nov (version 7): no 15:29 sale -- carried to the next session, the trail from its 09:15 open."""
    from app.services.backfill_platform.coverage import next_trading_day

    await market.record(at(4, 15, 15), put_day=1_00_000, call_day=4_00_000)
    market.deployment.state = {"last_mark": at(4, 15, 0).isoformat()}
    bought = await market.check(at(4, 15, 15, 4))
    assert bought.state["position"]["option_type"] == "PE" and bought._wake_at == at(4, 15, 15, 5)
    carried = await market.check(at(4, 15, 29))
    nxt = next_trading_day(at(4, 15, 29).date())
    open_next = datetime.combine(nxt, oib.SESSION_OPEN, tzinfo=at(4, 9, 15).tzinfo)
    assert await market.trades() == [] and carried.state["position"]["option_type"] == "PE"
    assert carried._last_reason.startswith("carried overnight:") and carried._wake_at == open_next
    assert f"the trail and the next decision from the 09:15 open {nxt:%a %d %b}" in carried._last_reason
    assert oib.next_session_open(at(4, 15, 29)) == open_next and oib.next_session_open(at(5, 9, 0)) == at(5, 9, 15)
    later = await market.check(at(4, 15, 30))
    assert later.state["position"] is not None and await market.trades() == []
    morning = await market.check(at(5, 9, 16))
    assert morning.state["position"]["option_type"] == "PE" and "on 04 Nov" in morning._last_reason
    assert "waiting for the 09:15 record" in morning._last_reason and morning._wake_at == at(5, 9, 16, 1)  # no 09:00 row here
    await market.record(at(5, 9, 30), put_day=1_00_000, call_day=2_00_000)  # still the put side: kept
    kept = await market.check(at(5, 9, 30, 4))
    assert kept.state["position"]["option_type"] == "PE" and await market.trades() == []


async def test_a_carried_option_gapping_under_its_trail_is_sold_at_the_first_check(market):
    await market.record(at(4, 14, 0), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = {"last_mark": at(4, 13, 45).isoformat()}
    p = (await market.check(at(4, 14, 0, 4))).state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost * 1.30 + SPREAD  # +30%: the trail on
    await market.check(at(4, 14, 30))
    await market.check(at(4, 15, 29))  # carried
    assert await market.trades() == []
    market.premium[key] = cost * 1.05 + SPREAD  # a gap open under the stop
    out = await market.check(at(5, 9, 15, 1))
    [trade] = await market.trades()
    assert trade.exit_reason == "trailing_stop" and out.state.get("position") is None


async def test_on_its_expiry_day_the_held_option_is_rolled_to_next_weeks_at_noon(market):
    """25 Nov (version 7): carried into its expiry day (9 Nov), it's sold at 12:00 and the same side bought on next week's."""
    await market.record(at(6, 15, 15), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = {"last_mark": at(6, 15, 0).isoformat()}
    p = (await market.check(at(6, 15, 15, 4))).state["position"]
    assert p["weekly_expiry"] == W1.isoformat() and p["option_type"] == "CE"
    await market.check(at(6, 15, 29))  # carried over the weekend
    before = await market.check(at(9, 11, 59, 59))
    assert before.state["position"]["weekly_expiry"] == W1.isoformat() and await market.trades() == []
    assert "expires today: rolled to next week's at 12:00" in before._last_reason
    rolled = await market.check(at(9, 12, 0))
    [trade] = await market.trades()
    assert trade.exit_reason == "expiry_rollover" and rolled.state.get("position") is None
    assert rolled._last_reason.startswith("expiry rollover:") and rolled._wake_at == at(9, 12, 0, 5)
    await market.record(at(9, 12, 0), put_day=4_00_000, call_day=1_00_000)
    bought = await market.check(at(9, 12, 0, 5))
    q = bought.state["position"]
    assert (q["weekly_expiry"], q["option_type"], q["option"]) == (W2.isoformat(), "CE", "17 Nov 23,450 CE")


async def test_an_option_found_expired_is_rolled_at_the_first_check_and_one_held_at_1529_on_its_expiry_day_is_sold(market):
    await market.record(at(6, 15, 15), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = {"last_mark": at(6, 15, 0).isoformat()}
    ctx = await market.check(at(6, 15, 15, 4))
    saved = {**ctx.state, "position": {**ctx.state["position"]}}
    late = await market.check(at(11, 9, 20))  # the app down from Friday to Wednesday: 9 Nov's option has expired
    [trade] = await market.trades()
    assert trade.exit_reason == "expiry_rollover" and late.state["pending"]["option_type"] == "CE"

    market.deployment.state = saved  # the same option, the app down at 12:00 on its expiry day
    closed = await market.check(at(9, 15, 29))
    reasons = sorted(t.exit_reason for t in await market.trades())
    assert reasons == ["expiry_day_close", "expiry_rollover"] and closed.state.get("position") is None
    assert closed._last_reason.startswith("15:29 on its expiry day, not rolled at 12:00")


async def test_on_the_weeklys_expiry_day_it_buys_next_weeks(market):
    await market.record(at(9, 9, 30), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = {"last_mark": at(6, 15, 15).isoformat()}
    ctx = await market.check(at(9, 9, 30, 4))
    assert ctx.state["position"]["weekly_expiry"] == W2.isoformat() and ctx.state["position"]["option"] == "17 Nov 23,450 CE"


async def test_a_card_started_between_closes_follows_the_live_candle_at_once_and_a_buy_waits_for_a_book(market):
    await market.record(at(4, 10, 0), put_day=4_00_000, call_day=1_00_000)
    market.deployment.state = None  # a new card, started 10:07
    market.no_book = {(W1, 23450.0, "CE")}
    waits = await market.check(at(4, 10, 7))
    assert waits.state.get("position") is None and "no bid/ask for the 09 Nov 23,450 CE yet" in waits._last_reason
    assert waits._wake_at == at(4, 10, 7, 1)
    market.no_book = set()
    assert (await market.check(at(4, 10, 7, 1))).state["position"]["option"] == "09 Nov 23,450 CE"


async def test_the_card_values_the_option_at_the_bid_and_names_the_trade(market):
    from app.api.v1.endpoints import paper_native_trading as ep

    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    await market.check(at(4, 9, 30, 4))
    market.prices(at(4, 11, 0), 23460.0)
    out = await ep._build_position_out(market.db, market.deployment.state, at(4, 11, 0).astimezone(timezone.utc))
    [leg] = out.legs
    assert out.bias == "long call" and leg.current_price == leg.bid and leg.ask == leg.bid + 2 * SPREAD
    assert out.payoff is None and out.metrics["put_change_live"] == 6_50_000

    market.deployment.state = {**market.deployment.state, "force_exit": True}
    manual = await market.check(at(4, 11, 0))
    assert manual.state.get("position") is None and manual._last_reason.startswith("manual exit")
    rows = [(t, "OI Change Buy 15 MIN", "INR") for t in (await market.db.execute(select(PaperNativeTrade))).scalars().all()]
    [trade] = await ep._trade_outs(market.db, rows)
    assert trade.structure == "Long CE"


async def test_a_position_from_version_7_drops_its_averages_and_keeps_on(market):
    await market.record(at(4, 11, 30), put_day=4_38_984, call_day=2_47_377)
    market.deployment.state = {"last_mark": at(4, 11, 15).isoformat()}
    held = (await market.check(at(4, 11, 30, 6))).state["position"]
    for key in ("put_change_live", "call_change_live"):
        held.pop(key)
    market.deployment.state = {**market.deployment.state, "position": {**held, "put_avg": 3e5, "call_avg": 2e5, "put_change_15m": 1.0,
                                                                         "call_change_15m": 2.0}, "last_mark": at(4, 11, 15).isoformat()}
    kept = await market.check(at(4, 11, 40))
    p = kept.state["position"]
    assert {"put_avg", "call_avg", "put_change_15m", "call_change_15m"}.isdisjoint(p) and p["put_change_live"] == 4_38_984
    assert p["option_type"] == "CE" and "last_mark" not in kept.state and await market.trades() == []


async def test_the_trail_starts_at_plus_15_percent_then_sells_5_percent_under_the_highest_bid(market):
    """17 Nov (version 3): +15% / 5% on the bid, read every second; after it, flat until the next close."""
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    p = (await market.check(at(4, 9, 30, 4))).state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost * 1.14 + SPREAD  # a bid of +14%: not yet
    early = await market.check(at(4, 9, 35))
    assert early.state["position"]["trail_stop"] is None and "trail starts at a bid of" in early._last_reason
    market.premium[key] = cost * 1.30 + SPREAD  # +30%: on, the stop 5% under it
    on = (await market.check(at(4, 9, 36))).state["position"]
    assert on["peak_bid"] == pytest.approx(cost * 1.30, abs=0.01) and on["trail_stop"] == pytest.approx(cost * 1.30 * 0.95, abs=0.02)
    market.premium[key] = cost * 1.25 + SPREAD  # down a little, over the stop: held, the stop where it was
    held = (await market.check(at(4, 9, 37))).state["position"]
    assert held["trail_stop"] == on["trail_stop"] and await market.trades() == []
    market.premium[key] = cost * 1.22 + SPREAD  # under the stop: sold at the bid
    out = await market.check(at(4, 9, 38))
    [trade] = await market.trades()
    assert trade.exit_reason == "trailing_stop" and trade.legs[0]["exit_price"] == pytest.approx(cost * 1.22, abs=0.01)
    assert out.state.get("position") is None and out._last_reason.startswith("trailing stop:")
    again = await market.check(at(4, 9, 40))  # flat until the next close, whatever the live candle reads
    assert again.state.get("position") is None and "flat after the trailing stop until the 09:45 close" in again._last_reason
    await market.record(at(4, 9, 45), put_day=5_00_000, call_day=1_00_000)  # the same side again: bought again
    market.premium.clear()
    assert (await market.check(at(4, 9, 45, 4))).state["position"]["option_type"] == "CE"


async def test_a_stop_hit_at_a_close_waits_for_the_next_one(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    p = (await market.check(at(4, 9, 30, 4))).state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost * 1.40 + SPREAD
    await market.check(at(4, 9, 44))
    market.premium[key] = cost * 1.20 + SPREAD
    await market.record(at(4, 9, 45), put_day=5_00_000, call_day=1_00_000)
    out = await market.check(at(4, 9, 45, 2))  # the stop hit just after the 09:45 close: that close isn't bought on
    assert out.state.get("position") is None and out.state["flat_until"] == at(4, 10, 0).astimezone(timezone.utc).isoformat()
    assert (await market.check(at(4, 9, 45, 5))).state.get("position") is None
    assert (await market.check(at(4, 10, 0, 1))).state["position"]["option_type"] == "CE"  # the next close: the live candle again


async def test_version_8_flips_the_moment_the_live_candle_turns_mid_candle(market):
    """26 Nov: no average -- the live candle's call side higher at 09:37 sells the call then and buys the put 5 seconds later."""
    await market.record(at(4, 9, 30), put_day=6_00_000, call_day=1_00_000)
    await market.check(at(4, 9, 30, 4))
    market.reads(at(4, 9, 30), put=1_00_000, call=3_00_000)
    sold = await market.check(at(4, 9, 37))
    [trade] = await market.trades()
    assert trade.exit_reason == "oi_flip" and sold.state.get("position") is None and sold._wake_at == at(4, 9, 37, 5)
    assert "live 09:37:00 (since 09:30) change in OI: put +100,000, call +300,000 -- now a put" in sold._last_reason
    bought = await market.check(at(4, 9, 37, 5))
    assert bought.state["position"]["option_type"] == "PE"


async def test_a_buy_takes_the_side_the_live_candle_reads_when_it_is_bought(market):
    await market.record(at(4, 9, 30), put_day=6_00_000, call_day=1_00_000)
    await market.check(at(4, 9, 30, 4))
    market.reads(at(4, 9, 30), put=1_00_000, call=3_00_000)
    await market.check(at(4, 9, 37))  # the call sold, a put due at 09:37:05
    market.reads(at(4, 9, 30), put=5_00_000, call=3_00_000)  # back to the put side within the 5 seconds
    turned = await market.check(at(4, 9, 37, 3))
    assert turned.state["pending"]["option_type"] == "CE"
    bought = await market.check(at(4, 9, 37, 5))
    assert bought.state["position"]["option_type"] == "CE" and [t.exit_reason for t in await market.trades()] == ["oi_flip"]


async def test_its_trades_are_charged_no_brokerage_only_the_taxes_and_fees(market):
    """26 Nov (version 8): BROKERAGE_PER_ORDER 0, as Kotak's intraday."""
    from app.services.paper_trading.trade_record import estimate_charges

    await market.record(at(4, 9, 30), put_day=6_00_000, call_day=1_00_000)
    await market.check(at(4, 9, 30, 4))
    market.reads(at(4, 9, 30), put=1_00_000, call=3_00_000)
    await market.check(at(4, 9, 37))
    [trade] = await market.trades()
    legs, opened, closed = trade.legs, trade.opened_at, trade.closed_at
    assert trade.charges == estimate_charges(legs, opened, closed, 0.0)
    assert estimate_charges(legs, opened, closed) == pytest.approx(trade.charges + 2 * 20 * 1.18, abs=0.02)  # Rs 20 an order + GST
    assert 0 < trade.charges < 20


async def test_version_6_has_no_stop_loss_only_the_trail(market):
    """24 Nov: version 5's Rs 1,200 a lot is gone -- a loss of any size before the trail is held to a flip or 15:29."""
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    p = (await market.check(at(4, 9, 30, 4))).state["position"]
    cost, key = p["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost - 40 + SPREAD  # Rs 2,600 down at the bid
    held = await market.check(at(4, 9, 36))
    assert held.state["position"] is not None and await market.trades() == []
    assert "stop-loss" not in held._last_reason and "trail starts at a bid of" in held._last_reason


async def test_a_position_from_version_5_loses_its_stop_loss(market):
    await market.record(at(4, 9, 30), put_day=6_50_000, call_day=2_10_000)
    ctx = await market.check(at(4, 9, 30, 4))
    ctx.state["position"]["stop_loss_at"] = ctx.state["position"]["legs"]["long"]["entry_price"] - 18.46  # as version 5 left it
    market.deployment.state = ctx.state
    cost, key = ctx.state["position"]["legs"]["long"]["entry_price"], (W1, 23450.0, "CE")
    market.premium[key] = cost - 25 + SPREAD
    await market.check(at(4, 9, 33))
    assert await market.trades() == []
    await market.record(at(4, 9, 45), put_day=7_00_000, call_day=1_00_000)
    kept = (await market.check(at(4, 9, 45, 4))).state["position"]
    assert "stop_loss_at" not in kept and kept["option_type"] == "CE"


async def test_before_the_0915_open_nothing_is_decided(market):
    market.deployment.state = {"last_mark": at(3, 15, 15).isoformat()}
    await market.record(at(4, 9, 0), put_day=9_00_000, call_day=1_00_000)
    early = await market.check(at(4, 9, 14, 50))
    assert early.state.get("position") is None and early._wake_at == at(4, 9, 15) and "opened_on" not in early.state
    assert "next decision at the 09:15 open (the 09:00 row's change in OI), then on the live candle" in early._last_reason
    day_done = await market.check(at(4, 15, 30))
    assert day_done._last_reason.startswith("day done") and "at the 09:15 open" in day_done._last_reason


async def test_the_cards_chart_the_candles_and_the_side_each_live_candle_ended_on(market, db_session):
    """24 Nov, version 8 26 Nov: the chart under the card -- the latest session's candles, the 4 before its first,
    and the side each candle the strategy followed live (version 9: 09:15-15:29, those closing 09:30-15:30) ended on."""
    from fastapi import HTTPException

    from app.api.v1.endpoints import paper_native_trading as ep
    from app.models.paper_trading import PaperPortfolio
    from app.models.strategy import Strategy
    from app.models.user import User
    from app.services.strategy import oi_change_series as series

    marks = [at(4, 14, 30), at(4, 14, 45), at(4, 15, 0), at(4, 15, 15), at(4, 15, 30)]
    for i, mark in enumerate(marks):
        await market.record(mark, put_day=float(100 * i), call_day=50.0)
    await market.record(at(5, 9, 0), put_day=-9e6, call_day=9e6)  # overnight and pre-open: not candles
    await market.record(at(5, 9, 15), put_day=-9e6, call_day=9e6)
    await market.record(at(5, 9, 30), put_day=300.0, call_day=100.0)
    await market.record(at(5, 9, 45), put_day=0.0, call_day=500.0)
    out = await series.oi_change_series(market.db, at(5, 10, 0).astimezone(timezone.utc))
    assert out["average_candles"] == 1 and out["session"] == at(5, 9, 30).date()
    candles = out["candles"]
    assert [c["at"] for c in candles] == [m.astimezone(timezone.utc) for m in (*marks[1:], at(5, 9, 30), at(5, 9, 45))]
    assert [c["side"] for c in candles] == [None, None, None, None, "CE", "PE"]  # the day before shows no side
    assert (candles[5]["put_avg"], candles[5]["call_avg"]) == (0.0, 500.0)  # a candle is its own

    owner = await db_session.get(User, (await db_session.get(PaperPortfolio, market.portfolio.id)).user_id)
    strategy = await db_session.get(Strategy, market.deployment.strategy_id)
    strategy.name = "OI Change Buy 15 MIN"
    await db_session.commit()
    panel = await ep.native_deployment_oi_change(str(market.deployment.id), db=db_session, user=owner)
    assert panel.average_candles == 1 and len(panel.candles) >= 2
    strategy.name = "Conti Calendar 15 MIN"
    await db_session.commit()
    with pytest.raises(HTTPException) as err:
        await ep.native_deployment_oi_change(str(market.deployment.id), db=db_session, user=owner)
    assert err.value.status_code == 404


def test_its_trades_dont_go_to_telegram():
    from app.services.notifications.trade_alerts import alerted_strategies

    assert "oi change buy 15 min" not in alerted_strategies()
