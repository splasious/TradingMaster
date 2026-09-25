"""15-minute PCR records (services/options/pcr_snapshots.py): marks, live
capture over ATM ±40 of 4 expiries, like-for-like ΔOI, positioning, gap
fill from history, the continuous read, and compute_effective_pcr reading
the records first."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backfill_platform import BfSymbol
from app.models.pcr import SOURCE_HISTORICAL, SOURCE_LIVE, PcrSnapshot, PcrStrikeOi
from app.services.options import pcr_snapshots as ps
from app.services.options.pcr import compute_effective_pcr

IST = timezone(timedelta(hours=5, minutes=30))
EXPIRIES = [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 19), date(2026, 10, 27)]
STRIKES = [19000 + 50 * i for i in range(201)]  # 19000..29000
EPOCH = datetime(2026, 9, 21, 9, 0, tzinfo=IST)


def at(d: int, hh: int, mm: int) -> datetime:
    return datetime(2026, 9, d, hh, mm, tzinfo=IST).astimezone(timezone.utc)


def blocks(t: datetime) -> int:
    return int((t - EPOCH).total_seconds() // 900)


CE_RATE, PE_RATE = 1_000, 3_000  # OI added per contract every 15 minutes


def oi_of(option_type: str, strike: float, t: datetime) -> float:
    n = blocks(t)
    return (100_000 + strike / 10 + CE_RATE * n) if option_type == "CE" else (200_000 + strike / 10 + PE_RATE * n)


class FakeKite:
    """Kite's NFO dump, quotes and 15m history for a synthetic NIFTY chain
    whose OI grows by a fixed amount every 15 minutes."""

    def __init__(self, spot_fn):
        self.spot_fn = spot_fn
        self.now: datetime | None = None
        self.history_calls = 0
        self.rows = [
            {
                "instrument_token": str(1000 + i), "tradingsymbol": f"NIFTY{e:%y%m%d}{int(s)}{t}", "name": "NIFTY",
                "expiry": e.isoformat(), "strike": f"{s}.0", "instrument_type": t, "lot_size": "75",
            }
            for i, (e, s, t) in enumerate((e, s, t) for e in EXPIRIES for s in STRIKES for t in ("CE", "PE"))
        ]
        self.by_symbol = {r["tradingsymbol"]: r for r in self.rows}

    async def get_instruments(self, segment="NSE"):
        return self.rows

    async def get_ltp(self, exchange, tradingsymbol):
        return {"price": self.spot_fn(self.now)}

    async def get_quote_batch(self, instruments):
        out = {}
        for key in instruments:
            row = self.by_symbol[key.split(":", 1)[1]]
            out[key] = {"oi": oi_of(row["instrument_type"], float(row["strike"]), self.now), "last_price": 10.0, "volume": 5}
        return out

    async def get_historical_data(self, symbol, timeframe, start, end, segment="NSE", instrument_token=None):
        self.history_calls += 1
        candles = []
        d = start.astimezone(IST).date()
        while d <= end.astimezone(IST).date():
            if d.weekday() < 5:
                t = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
                while t.time() <= datetime(2026, 1, 1, 15, 15).time():
                    close_t = t + timedelta(minutes=15)
                    if close_t <= end:
                        if segment == "NSE":
                            candles.append({"ts": t, "open": self.spot_fn(t), "close": self.spot_fn(close_t), "open_interest": None})
                        else:
                            row = self.by_symbol[symbol]
                            candles.append({"ts": t, "open": 1.0, "close": 2.0,
                                            "open_interest": oi_of(row["instrument_type"], float(row["strike"]), close_t)})
                    t = close_t
            d += timedelta(days=1)
        return candles


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    monkeypatch.setattr(ps, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setattr(ps, "HISTORY_PAUSE_SECONDS", 0)


async def capture(db, kite, mark):
    kite.now = mark
    return await ps.capture_live(db, kite, "NIFTY", mark)


# ------------------------------------------------------------------ marks


def test_session_has_27_marks_0900_to_1530():
    marks = ps.session_marks(date(2026, 9, 25))
    assert len(marks) == 27 == ps.MARKS_PER_SESSION
    assert marks[0] == at(25, 9, 0) and marks[-1] == at(25, 15, 30)


def test_marks_continue_across_the_weekend():
    monday_0910 = at(28, 9, 10)
    assert ps.latest_mark(monday_0910) == at(28, 9, 0)
    marks = ps.expected_marks(monday_0910, limit=3)
    assert marks == [at(28, 9, 0), at(25, 15, 30), at(25, 15, 15)]
    assert ps.latest_mark(datetime(2026, 9, 27, 12, 0, tzinfo=IST)) == at(25, 15, 30)


def test_capture_is_due_only_within_the_grace_after_a_mark():
    assert ps.due_capture_mark(at(25, 10, 0) + timedelta(seconds=5)) == at(25, 10, 0)
    assert ps.due_capture_mark(at(25, 10, 3)) is None
    assert ps.due_capture_mark(datetime(2026, 9, 26, 10, 0, 5, tzinfo=IST)) is None  # Saturday


# ------------------------------------------------------------ positioning


@pytest.mark.parametrize(
    ("put", "call", "spot", "expected"),
    [
        (5e5, 1e5, 10, ("bullish", "put_led_buildup")),
        (5e5, -1e5, 10, ("bullish", "put_buildup_call_unwinding")),
        (0, -3e5, 5, ("bullish", "call_unwinding")),
        (1e5, 5e5, -10, ("bearish", "call_led_buildup")),
        (-2e5, 3e5, -1, ("bearish", "call_buildup_put_unwinding")),
        (5e5, 1e5, -10, ("divergence", "put_led_buildup")),
        (1e5, 5e5, 10, ("divergence", "call_led_buildup")),
        (-1e5, -2e5, 10, ("unwinding", "both_unwinding")),
        (100, 50, 10, ("flat", "flat")),
        (None, 1e5, 10, (None, None)),
    ],
)
def test_positioning_rules(put, call, spot, expected):
    assert ps.classify(put, call, spot, total_oi=1e8) == expected


# ---------------------------------------------------------------- capture


async def test_live_capture_sums_atm_40_over_4_expiries(db_session: AsyncSession):
    kite = FakeKite(lambda t: 23512.0)
    snap = await capture(db_session, kite, at(25, 10, 0))

    assert snap.source == SOURCE_LIVE
    assert snap.expiries == [e.isoformat() for e in EXPIRIES[:4]]
    assert snap.atm_strike == 23500 and snap.strike_step == 50
    assert snap.contracts_expected == 81 * 2 * 4 == snap.contracts_with_oi
    strikes = [s for s in STRIKES if 21500 <= s <= 25500]
    call = sum(oi_of("CE", s, at(25, 10, 0)) for s in strikes) * 4
    put = sum(oi_of("PE", s, at(25, 10, 0)) for s in strikes) * 4
    assert snap.total_call_oi == pytest.approx(call) and snap.total_put_oi == pytest.approx(put)
    assert snap.pcr == pytest.approx(put / call)
    assert [(r.expiry, r.strike_lo, r.strike_hi) for r in snap.expiry_rows] == [(e, 21500, 25500) for e in EXPIRIES[:4]]
    # ±60 captured, so the next record can compare the same contracts.
    stored = (await db_session.execute(select(func.count()).select_from(PcrStrikeOi))).scalar_one()
    assert stored == 121 * 2 * 4
    # Nothing to compare with yet.
    assert snap.call_oi_change is None and snap.positioning is None
    # A second capture of the same mark is a no-op.
    assert await capture(db_session, kite, at(25, 10, 0)) is None


async def test_oi_change_compares_the_same_contracts_after_an_atm_shift(db_session: AsyncSession):
    spots = {at(25, 10, 0): 23500.0, at(25, 10, 15): 23600.0}
    kite = FakeKite(lambda t: spots[t])
    first = await capture(db_session, kite, at(25, 10, 0))
    second = await capture(db_session, kite, at(25, 10, 15))

    # Each contract gained exactly CE_RATE / PE_RATE; 81 strikes x 4 expiries.
    assert second.call_oi_change == pytest.approx(81 * 4 * CE_RATE)
    assert second.put_oi_change == pytest.approx(81 * 4 * PE_RATE)
    assert second.oi_change_contracts == 81 * 2 * 4
    assert second.oi_change_pcr == pytest.approx(3.0)
    # The naive total difference would count the 2 strikes entering the window.
    assert second.total_call_oi - first.total_call_oi != pytest.approx(second.call_oi_change)
    assert second.atm_shift == 100 and second.spot_change == 100
    assert second.spot_change_pct == pytest.approx(100 / 23500 * 100)
    assert second.pcr_change == pytest.approx(second.pcr - first.pcr)
    assert second.prev_ts.replace(tzinfo=timezone.utc) == at(25, 10, 0)
    assert (second.positioning, second.oi_driver) == ("bullish", "put_led_buildup")
    assert "gap_before" not in second.flags


async def test_flags_and_day_change(db_session: AsyncSession):
    kite = FakeKite(lambda t: 23500.0)
    await capture(db_session, kite, at(24, 15, 30))
    opening = await capture(db_session, kite, at(25, 9, 0))
    later = await capture(db_session, kite, at(25, 9, 45))

    assert "pre_open" in opening.flags and "gap_before" not in opening.flags
    assert "gap_before" in later.flags and "pre_open" not in later.flags
    # Since the previous session's last record.
    n = blocks(at(25, 9, 45)) - blocks(at(24, 15, 30))
    assert later.day_baseline_ts.replace(tzinfo=timezone.utc) == at(24, 15, 30)
    assert later.call_oi_change_day == pytest.approx(81 * 4 * CE_RATE * n)
    assert later.put_oi_change_day == pytest.approx(81 * 4 * PE_RATE * n)


# --------------------------------------------------------------- gap fill


async def test_gap_fill_fills_todays_missing_marks_from_history(db_session: AsyncSession):
    kite = FakeKite(lambda t: 23500.0 + (50 if t >= at(25, 9, 30) else 0))
    await capture(db_session, kite, at(24, 15, 30))
    live = await capture(db_session, kite, at(25, 10, 15))
    assert "gap_before" in live.flags

    result = await ps.fill_gaps(db_session, kite, "NIFTY", at(25, 10, 25), sessions=1)
    assert result == {"filled": 5, "unfillable": 0, "missing": 5}  # 09:00 .. 10:00

    snaps = (await db_session.execute(select(PcrSnapshot).order_by(PcrSnapshot.ts))).scalars().all()
    assert [s.ts.replace(tzinfo=timezone.utc) for s in snaps] == [at(24, 15, 30)] + [at(25, 9, m) for m in (0, 15, 30, 45)] + [at(25, 10, 0), at(25, 10, 15)]
    filled = {s.ts.replace(tzinfo=timezone.utc): s for s in snaps}
    assert filled[at(25, 9, 0)].source == SOURCE_HISTORICAL
    # 09:00 / 09:15: the OI of the previous session's close; spot from the index.
    assert filled[at(25, 9, 0)].call_oi_change == pytest.approx(0)
    assert filled[at(25, 9, 15)].spot == 23500.0 and filled[at(25, 9, 30)].spot == 23550.0
    # 10:00 is the candle 09:45-10:00's close.
    assert filled[at(25, 10, 0)].call_oi_change == pytest.approx(81 * 4 * CE_RATE)
    # The live record now compares with the filled 10:00, not 15:30.
    live = filled[at(25, 10, 15)]
    await db_session.refresh(live)
    assert live.prev_ts.replace(tzinfo=timezone.utc) == at(25, 10, 0)
    assert "gap_before" not in live.flags
    assert live.call_oi_change == pytest.approx(81 * 4 * CE_RATE)

    # Nothing left: no more history calls.
    calls = kite.history_calls
    assert await ps.fill_gaps(db_session, kite, "NIFTY", at(25, 10, 25), sessions=1) == {"filled": 0, "unfillable": 0, "missing": 0}
    assert kite.history_calls == calls


async def test_sessions_whose_expiry_has_passed_are_not_filled(db_session: AsyncSession):
    db_session.add(BfSymbol(source="zerodha_nfo", symbol="NIFTY26922X", display_name="x", expiry=date(2026, 9, 24), underlying_symbol="NIFTY"))
    await db_session.commit()
    assert ps.fillable(date(2026, 9, 25), date(2026, 9, 25), {date(2026, 9, 24)})
    assert not ps.fillable(date(2026, 9, 24), date(2026, 9, 25), {date(2026, 9, 24)})

    kite = FakeKite(lambda t: 23500.0)
    result = await ps.fill_gaps(db_session, kite, "NIFTY", at(25, 9, 50), sessions=3)
    # 23 and 24 Sep (27 marks each) had the 24 Sep expiry; today's 4 are filled.
    assert result == {"filled": 4, "unfillable": 54, "missing": 58}


# ------------------------------------------------------------------- read


async def test_rows_are_continuous_with_missing_and_pending_marks(db_session: AsyncSession):
    kite = FakeKite(lambda t: 23500.0)
    for mark in (at(25, 9, 0), at(25, 9, 15), at(25, 9, 45)):
        await capture(db_session, kite, mark)

    rows = await ps.snapshot_rows(db_session, "NIFTY", at(25, 10, 0) + timedelta(seconds=30), limit=25)
    assert [(r["ts"], r["status"]) for r in rows] == [
        (at(25, 10, 0), "pending"), (at(25, 9, 45), "recorded"), (at(25, 9, 30), "missing"),
        (at(25, 9, 15), "recorded"), (at(25, 9, 0), "recorded"),
    ]  # nothing before the first record
    assert rows[1]["pcr"] is not None and rows[2].get("pcr") is None

    ranged = await ps.snapshot_rows(db_session, "NIFTY", at(25, 16, 0), start=date(2026, 9, 25), end=date(2026, 9, 25), include_expiries=True)
    assert len(ranged) == 27 and ranged[0]["ts"] == at(25, 15, 30)
    assert len(ranged[-1]["expiry_rows"]) == 4


async def test_effective_pcr_reads_the_latest_record(db_session: AsyncSession):
    kite = FakeKite(lambda t: 23500.0)
    assert await compute_effective_pcr(db_session) is None  # no records, no catalog
    a = await capture(db_session, kite, at(25, 9, 0))
    b = await capture(db_session, kite, at(25, 9, 15))
    assert await compute_effective_pcr(db_session, as_of=at(25, 9, 10)) == pytest.approx(a.pcr)
    assert await compute_effective_pcr(db_session, as_of=at(25, 9, 20)) == pytest.approx(b.pcr)
    # Other scopes keep the roll-up.
    assert await compute_effective_pcr(db_session, num_expiries=2, as_of=at(25, 9, 20)) is None


async def test_snapshots_endpoint(client, seeded_admin, db_session: AsyncSession):
    resp = await client.post("/api/v1/auth/login", json={"email": seeded_admin["email"], "password": seeded_admin["password"]})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    kite = FakeKite(lambda t: 23500.0)
    await capture(db_session, kite, at(25, 15, 30))

    resp = await client.get("/api/v1/options/pcr/snapshots", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["strike_window"] == 40 and body["expiries_summed"] == 4 and body["marks_per_session"] == 27
    assert body["rows"][-1]["status"] == "recorded" and body["rows"][-1]["contracts_expected"] == 648
    assert "last_error" in body["capture"]

    assert (await client.get("/api/v1/options/pcr/snapshots?underlying=BANKNIFTY", headers=headers)).status_code == 404


# -------------------------------------------------------------- scheduler


async def test_scheduler_waits_for_login_then_captures_the_due_mark(db_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.options import pcr_snapshot_scheduler as sched

    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(sched, "AsyncSessionLocal", factory)
    kite = FakeKite(lambda t: 23500.0)
    mark = at(25, 10, 0)

    async def not_logged_in(db):
        return None

    async def logged_in(db):
        kite.now = mark
        return kite

    scheduler = sched.PcrSnapshotScheduler()
    monkeypatch.setattr(sched, "kite_broker", not_logged_in)
    await scheduler.tick(mark + timedelta(seconds=5))
    assert scheduler.last_error == sched.NOT_LOGGED_IN and scheduler.last_capture_ts is None

    monkeypatch.setattr(sched, "kite_broker", logged_in)
    await scheduler.tick(mark + timedelta(seconds=10))
    assert scheduler.last_capture_ts == mark and scheduler.last_error is None
    async with factory() as db:
        assert (await db.execute(select(func.count()).select_from(PcrSnapshot))).scalar_one() == 1
