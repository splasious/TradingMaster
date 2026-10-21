"""NIFTY top-3 OTM open interest alert (services/options/oi_levels.py): a
price-ladder picture (oi_levels_chart.py) with a short summary, or the
full table as text when the picture can't be drawn."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.options import oi_levels, oi_levels_chart
from tests.test_pcr_snapshots import EXPIRIES, FakeKite, at, capture

NEAR = EXPIRIES[0]  # 29 Sep
LAKH = 100_000

# The nearest expiry's stacked strikes, in lakh, per mark (everything else
# holds 10 lakh). The 22,500 CE and 22,600 PE are in the money at a NIFTY
# of 22,5xx, so their larger OI is never a level.
STACKS = {
    "prev_close": {("CE", 22600): 58.2, ("CE", 23000): 75.2, ("CE", 22800): 56.2,
                   ("PE", 22500): 50.3, ("PE", 22000): 79.0, ("PE", 22300): 53.6},
    "10:00": {("CE", 22600): 86.3, ("CE", 23000): 90.0, ("CE", 22800): 62.9,
              ("PE", 22500): 83.7, ("PE", 22000): 85.1, ("PE", 22300): 54.9},
    "10:15": {("CE", 22600): 98.4, ("CE", 23000): 85.2, ("CE", 22800): 61.7, ("CE", 22500): 200.0,
              ("PE", 22500): 102.3, ("PE", 22000): 90.1, ("PE", 22300): 55.8, ("PE", 22600): 150.0},
}


class StackedKite(FakeKite):
    def __init__(self, spots: dict, stacks: dict):
        super().__init__(lambda t: spots[t])
        self.stacks = stacks

    async def get_quote_batch(self, instruments):
        out = {}
        for key in instruments:
            row = self.by_symbol[key.split(":", 1)[1]]
            lakh = 10.0
            if row["expiry"] == NEAR.isoformat():
                lakh = self.stacks[self.now].get((row["instrument_type"], int(float(row["strike"]))), 10.0)
            out[key] = {"oi": lakh * LAKH, "last_price": 10.0, "volume": 5}
        return out


def test_top_levels_are_the_highest_oi_out_of_the_money_strikes():
    e = date(2026, 10, 20)
    oi_map = {
        (e, 22600.0, "CE"): 9.0, (e, 22700.0, "CE"): 5.0, (e, 22800.0, "CE"): 9.0, (e, 22900.0, "CE"): 7.0,
        (e, 22400.0, "CE"): 99.0,  # in the money
        (e, 22400.0, "PE"): 8.0, (e, 22300.0, "PE"): None, (e, 22700.0, "PE"): 99.0,  # no OI / in the money
        (date(2026, 10, 27), 22600.0, "CE"): 50.0,  # another expiry
    }
    calls = oi_levels.top_levels(oi_map, e, 22550.0, "CE", prev_map={(e, 22600.0, "CE"): 7.0})
    assert [(lvl.strike, lvl.oi) for lvl in calls] == [(22600.0, 9.0), (22800.0, 9.0), (22900.0, 7.0)]  # nearer first on a tie
    assert (calls[0].change, calls[1].change, calls[0].change_day) == (2.0, None, None)
    assert [lvl.strike for lvl in oi_levels.top_levels(oi_map, e, 22550.0, "PE")] == [22400.0]


@pytest.mark.parametrize(
    "hh, mm, every, due",
    [(9, 0, 15, False), (9, 15, 15, True), (12, 30, 15, True), (15, 30, 15, True),
     (9, 45, 30, True), (10, 0, 30, False), (10, 15, 60, True), (10, 15, 0, False)],
)
def test_alert_due_from_0915_every_n_minutes(hh, mm, every, due):
    assert oi_levels.alert_due(at(25, hh, mm), every) is due


async def _three_records(db: AsyncSession):
    marks = {"prev_close": at(24, 15, 30), "10:00": at(25, 10, 0), "10:15": at(25, 10, 15)}
    spots = {marks["prev_close"]: 22470.0, marks["10:00"]: 22525.3, marks["10:15"]: 22560.5}
    kite = StackedKite(spots, {marks[k]: v for k, v in STACKS.items()})
    for name in ("prev_close", "10:00", "10:15"):
        snap = await capture(db, kite, marks[name])
    return snap


async def test_the_message_lists_resistance_and_support_with_their_oi_and_changes(db_session: AsyncSession):
    snap = await _three_records(db_session)
    subject, body = await oi_levels.oi_levels_message(db_session, snap)

    assert subject == "NIFTY OI levels 10:15 · S 22,500 / R 22,600"
    lines = body.splitlines()
    assert lines[0].startswith("NIFTY 22,560.5 (+35.2) PCR ")
    assert lines[1:] == [
        "",
        "Expiry 29 Sep",
        "RESISTANCE: call OI above NIFTY",
        "# Strike OI(L)  Δ15m   ΔDay Away",
        "1 22,600  98.4 +12.1  +40.2  +40",  # lowest strike first, # = OI rank
        "3 22,800  61.7  -1.2   +5.5 +240",
        "2 23,000  85.2  -4.8  +10.0 +440",
        "SUPPORT: put OI below NIFTY",
        "# Strike OI(L)  Δ15m   ΔDay Away",
        "2 22,000  90.1  +5.0  +11.1 -560",
        "3 22,300  55.8  +0.9   +2.2 -260",
        "1 22,500 102.3 +18.6  +52.0  -60",
        "Range 22,500 – 22,600",
        "Shift R: 23,000 → 22,600",
        "Shift S: 22,000 → 22,500",
        "",
        "OI(L) in lakh · Away in points",
        "ΔDay = since previous close",
    ]
    # Fits a group's narrower block on a phone, where 36 characters wrapped (15 Oct).
    assert max(len(line) for line in lines) <= oi_levels.MAX_WIDTH == 32


async def test_on_expiry_day_the_next_expiry_is_shown_too(db_session: AsyncSession):
    mark = at(29, 11, 0)  # 29 Sep is the nearest expiry
    kite = StackedKite({mark: 22560.5}, {mark: STACKS["10:15"]})
    snap = await capture(db_session, kite, mark)
    _, body = await oi_levels.oi_levels_message(db_session, snap)
    assert "Expiry 29 Sep (today)" in body and "Expiry 06 Oct" in body
    assert "Δprev" in body and "Shift" not in body  # nothing before it to compare with
    assert max(len(line) for line in body.splitlines()) <= oi_levels.MAX_WIDTH


async def test_the_scheduler_sends_it_after_saving_a_due_record(db_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.options import pcr_snapshot_scheduler as sched

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(sched, "AsyncSessionLocal", factory)
    sent = []

    async def fake_send(subject, body, to_group=False, photo=None):
        sent.append((subject, body, to_group))
        assert photo[:4] == b"\x89PNG"  # the picture, the summary under it

    monkeypatch.setattr(oi_levels, "send_telegram", fake_send)
    marks = [at(25, 9, 0), at(25, 9, 15)]
    kite = StackedKite({m: 22560.5 for m in marks}, {m: STACKS["10:15"] for m in marks})

    async def logged_in(db):
        return kite

    monkeypatch.setattr(sched, "kite_broker", logged_in)
    scheduler = sched.PcrSnapshotScheduler()
    for mark in marks:
        kite.now = mark
        await scheduler.tick(mark + timedelta(seconds=5))
        assert scheduler.last_capture_ts == mark
    # 09:00 is the pre-open; 09:15 is the first alert.
    assert [(s, to_group) for s, _, to_group in sent] == [("NIFTY OI levels 09:15 · S 22,500 / R 22,600", True)]  # the group too

    # Turned off, or failing: the record is still saved and counted.
    monkeypatch.setattr(get_settings(), "oi_levels_alert_minutes", 0)
    mark = at(25, 9, 30)
    kite.spot_fn = lambda t: 22560.5
    kite.stacks[mark] = STACKS["10:15"]
    kite.now = mark
    await scheduler.tick(mark + timedelta(seconds=5))
    assert scheduler.last_capture_ts == mark and len(sent) == 1

    monkeypatch.setattr(get_settings(), "oi_levels_alert_minutes", 15)

    async def broken_send(subject, body, to_group=False, photo=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(oi_levels, "send_telegram", broken_send)
    mark = at(25, 9, 45)
    kite.stacks[mark] = STACKS["10:15"]
    kite.now = mark
    await scheduler.tick(mark + timedelta(seconds=5))
    assert scheduler.last_capture_ts == mark and scheduler.last_error is None


async def test_the_picture_ladders_each_expirys_levels_and_the_caption_sums_them_up(db_session: AsyncSession):
    snap = await _three_records(db_session)
    sections, change_label = await oi_levels._sections(db_session, snap)
    chart = oi_levels.chart_of(snap, sections, change_label)
    [panel] = chart.panels
    assert (chart.change_text, panel.title) == ("15m", "Expiry 29 Sep")
    assert [(b.rank, b.strike, b.oi, b.change, b.change_day) for b in panel.resistance] == [
        (1, 22600, 98.4, pytest.approx(12.1), pytest.approx(40.2)), (2, 23000, 85.2, pytest.approx(-4.8), pytest.approx(10.0)),
        (3, 22800, 61.7, pytest.approx(-1.2), pytest.approx(5.5))]
    assert panel.notes == ["Range 22,500 – 22,600", "▲ Support moved up  22,000 → 22,500", "▼ Resistance moved down  23,000 → 22,600"]
    assert chart.summary == "29 Sep: NIFTY 22,560 is 40 pts below resistance 22,600 and 60 pts above support 22,500."
    png = oi_levels_chart.draw(chart)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000

    subject, body = oi_levels.caption(snap, sections)
    assert subject == "NIFTY OI levels 10:15 · S 22,500 / R 22,600"
    assert body.splitlines()[1:] == ["29 Sep  S 22,500 · R 22,600", "29 Sep  S moved 22,000→22,500", "29 Sep  R moved 23,000→22,600"]
    assert max(len(line) for line in body.splitlines()) <= oi_levels.MAX_WIDTH


async def test_on_expiry_day_the_picture_has_both_expiries(db_session: AsyncSession):
    mark = at(29, 11, 0)
    kite = StackedKite({mark: 22560.5}, {mark: STACKS["10:15"]})
    snap = await capture(db_session, kite, mark)
    sections, change_label = await oi_levels._sections(db_session, snap)
    chart = oi_levels.chart_of(snap, sections, change_label)
    assert [p.title for p in chart.panels] == ["Expiry 29 Sep · today", "Expiry 06 Oct"]
    assert all(b.change is None for p in chart.panels for b in p.resistance + p.support)  # nothing before it: no outlines
    assert oi_levels_chart.draw(chart)[:4] == b"\x89PNG"


async def test_a_picture_that_cant_be_drawn_sends_the_table(db_session: AsyncSession, monkeypatch):
    snap = await _three_records(db_session)
    sent = []

    async def fake_send(subject, body, to_group=False, photo=None):
        sent.append((body, photo))

    def broken(chart):
        raise RuntimeError("no fonts")

    monkeypatch.setattr(oi_levels, "send_telegram", fake_send)
    monkeypatch.setattr(oi_levels_chart, "draw", broken)
    monkeypatch.setattr(get_settings(), "oi_levels_alert_minutes", 15)
    assert await oi_levels.send_oi_levels(db_session, snap)
    [(body, photo)] = sent
    assert photo is None and "RESISTANCE: call OI above NIFTY" in body


async def _nifty_candles(db: AsyncSession, last: datetime) -> Instrument:
    """NIFTY 50's 15-minute candles: 24 Sep's from 13:15, then 25 Sep's from
    09:15 up to the one starting at `last`."""
    nifty = Instrument(exchange="NSE", symbol="NIFTY 50", name="NIFTY 50", instrument_type="index", data_source="zerodha_kite",
                       external_ref="NIFTY 50")
    db.add(nifty)
    await db.flush()
    starts = [at(24, 13, 15) + timedelta(minutes=15 * n) for n in range(9)]
    starts += [at(25, 9, 15) + timedelta(minutes=15 * n) for n in range(int((last - at(25, 9, 15)) / timedelta(minutes=15)) + 1)]
    for n, ts in enumerate(starts):
        close = 22450.0 + 10 * n
        db.add(OhlcvCandle(instrument_id=nifty.id, timeframe="15m", ts=ts.astimezone(timezone.utc), open=close - 10, high=close + 6,
                           low=close - 14, close=close, volume=None, source="test"))
    await db.commit()
    return nifty


async def test_the_picture_carries_niftys_candles_and_the_one_forming(db_session: AsyncSession):
    snap = await _three_records(db_session)  # 25 Sep 10:15, NIFTY 22,560.5
    await _nifty_candles(db_session, at(25, 10, 0))
    recent = await oi_levels._recent(db_session, snap)
    assert len(recent.candles) == 13 and recent.forming["close"] == 22560.5
    assert recent.forming["ts"] == at(25, 10, 15) and recent.forming["open"] == recent.candles[-1]["close"]
    sections, change_label = await oi_levels._sections(db_session, snap)
    profile = await oi_levels._profile(db_session, snap)
    chart = oi_levels.chart_of(snap, sections, change_label, recent, profile)
    assert chart.recent is recent and chart.profile == profile and oi_levels_chart.draw(chart)[:4] == b"\x89PNG"


async def test_the_oi_profile_is_the_nearest_expirys_out_of_the_money_oi_by_strike(db_session: AsyncSession):
    snap = await _three_records(db_session)  # NIFTY 22,560.5
    profile = {(row.strike, row.option_type): row.oi for row in await oi_levels._profile(db_session, snap)}
    assert profile[(22600.0, "CE")] == pytest.approx(98.4) and profile[(22500.0, "PE")] == pytest.approx(102.3)
    assert (22500.0, "CE") not in profile and (22600.0, "PE") not in profile  # in the money: their OI is no level
    assert {t for (k, t) in profile if k > snap.spot} == {"CE"} and {t for (k, t) in profile if k < snap.spot} == {"PE"}
    assert min(k for k, _ in profile) >= snap.spot - oi_levels.PROFILE_POINTS and max(k for k, _ in profile) <= snap.spot + 400
    assert len(profile) == 16  # 22,200 .. 22,950 every 50


async def test_the_candle_that_just_closed_may_be_missing_but_no_more(db_session: AsyncSession):
    snap = await _three_records(db_session)
    nifty = await _nifty_candles(db_session, at(25, 9, 45))  # 10:00 not saved yet
    recent = await oi_levels._recent(db_session, snap)
    assert recent.candles[-1]["ts"] == at(25, 9, 45) and recent.forming["open"] == 22560.5  # not following 09:45: opens at NIFTY
    await db_session.execute(delete(OhlcvCandle).where(OhlcvCandle.instrument_id == nifty.id, OhlcvCandle.ts == at(25, 9, 45)))
    await db_session.commit()
    assert await oi_levels._recent(db_session, snap) is None  # two behind: the ladders alone


async def test_a_live_record_waits_for_the_candle_that_closed_at_the_mark(db_session: AsyncSession, monkeypatch):
    snap = await _three_records(db_session)
    nifty = await _nifty_candles(db_session, at(25, 9, 45))
    mark = snap.ts if snap.ts.tzinfo else snap.ts.replace(tzinfo=timezone.utc)
    slept = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return mark + timedelta(seconds=20)

    async def sleep(seconds):  # the candle sync saves it meanwhile
        slept.append(seconds)
        db_session.add(OhlcvCandle(instrument_id=nifty.id, timeframe="15m", ts=at(25, 10, 0).astimezone(timezone.utc), open=22550.0,
                                   high=22566.0, low=22540.0, close=22558.0, volume=None, source="test"))
        await db_session.commit()

    monkeypatch.setattr(oi_levels, "datetime", Clock)
    monkeypatch.setattr(oi_levels.asyncio, "sleep", sleep)
    recent = await oi_levels._recent(db_session, snap)
    assert slept == [oi_levels.CANDLE_POLL_SECONDS] and recent.candles[-1]["close"] == 22558.0
    assert recent.forming["open"] == 22558.0
