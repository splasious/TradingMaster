"""NIFTY top-3 OTM open interest alert (services/options/oi_levels.py)."""

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.options import oi_levels
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
    assert lines[0].startswith("NIFTY 22,560.5 (+35.2)  PCR ")
    assert lines[1:] == [
        "",
        "Expiry 29 Sep",
        "RESISTANCE: call OI above NIFTY",
        "#  Strike  OI(L)   Δ15m   ΔDay  Away",
        "1  22,600   98.4  +12.1  +40.2   +40",
        "2  23,000   85.2   -4.8  +10.0  +440",
        "3  22,800   61.7   -1.2   +5.5  +240",
        "SUPPORT: put OI below NIFTY",
        "#  Strike  OI(L)   Δ15m   ΔDay  Away",
        "1  22,500  102.3  +18.6  +52.0   -60",
        "2  22,000   90.1   +5.0  +11.1  -560",
        "3  22,300   55.8   +0.9   +2.2  -260",
        "Range 22,500 – 22,600",
        "Shift: resistance 23,000 → 22,600",
        "Shift: support 22,000 → 22,500",
        "",
        "OI(L) lakh · Away = pts from NIFTY",
        "ΔDay = since previous close",
    ]
    assert max(len(line) for line in lines) <= 40  # readable on a phone


async def test_on_expiry_day_the_next_expiry_is_shown_too(db_session: AsyncSession):
    mark = at(29, 11, 0)  # 29 Sep is the nearest expiry
    kite = StackedKite({mark: 22560.5}, {mark: STACKS["10:15"]})
    snap = await capture(db_session, kite, mark)
    _, body = await oi_levels.oi_levels_message(db_session, snap)
    assert "Expiry 29 Sep (today)" in body and "Expiry 06 Oct" in body
    assert "Δprev" in body and "Shift" not in body  # nothing before it to compare with


async def test_the_scheduler_sends_it_after_saving_a_due_record(db_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.options import pcr_snapshot_scheduler as sched

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(sched, "AsyncSessionLocal", factory)
    sent = []

    async def fake_send(subject, body):
        sent.append((subject, body))

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
    assert [s for s, _ in sent] == ["NIFTY OI levels 09:15 · S 22,500 / R 22,600"]

    # Turned off, or failing: the record is still saved and counted.
    monkeypatch.setattr(get_settings(), "oi_levels_alert_minutes", 0)
    mark = at(25, 9, 30)
    kite.spot_fn = lambda t: 22560.5
    kite.stacks[mark] = STACKS["10:15"]
    kite.now = mark
    await scheduler.tick(mark + timedelta(seconds=5))
    assert scheduler.last_capture_ts == mark and len(sent) == 1

    monkeypatch.setattr(get_settings(), "oi_levels_alert_minutes", 15)

    async def broken_send(subject, body):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(oi_levels, "send_telegram", broken_send)
    mark = at(25, 9, 45)
    kite.stacks[mark] = STACKS["10:15"]
    kite.now = mark
    await scheduler.tick(mark + timedelta(seconds=5))
    assert scheduler.last_capture_ts == mark and scheduler.last_error is None
