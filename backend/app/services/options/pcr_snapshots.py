"""15-minute NIFTY PCR records (models/pcr.py): capture, gap fill, and the
"what changed since the previous record" fields.

A record is due at every 15-minute mark from 09:00 to 15:30 IST on NSE
trading days (27 a session) and shows the market as it was at that mark.

Live capture, a few seconds after the mark: NIFTY from Kite's quote, then
the OI of every contract within ATM ±CAPTURE_WINDOW strikes of the next
EXPIRIES expiries -- in one or two /quote calls (500 instruments each), so
it doesn't depend on the live ticker's 3,000-token budget. A mark not
captured within CAPTURE_GRACE is left to the gap fill: a quote taken later
would be the OI at the wrong moment.

Gap fill, from Kite's 15-minute history: a candle's OI is the OI when it
closed, and OI only moves when a contract trades, so the OI at a mark is
the last candle closed by then (for 09:00 and 09:15, the previous
session's last one). A session is filled only while all 4 of its expiries
are still listed -- Kite serves nothing for an expired contract, and a
record summed over 3 expiries would not compare with the others -- so a
missed session can be filled until the next expiry passes, and no later.

ΔOI is like-for-like: each contract's own change, summed over the
contracts in the current ±STRIKE_WINDOW window that were also captured in
the previous record. Capturing ±60 while summing ±40 keeps every contract
of the window comparable after the ATM moves, so a strike entering the
window never shows up as fresh build-up.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time import as_aware_utc
from app.models.backfill_platform import BfSymbol
from app.models.instrument import Instrument
from app.models.pcr import SOURCE_HISTORICAL, SOURCE_LIVE, PcrSnapshot, PcrSnapshotExpiry, PcrStrikeOi
from app.services.backfill_platform.coverage import IST, is_trading_day, previous_trading_day
from app.services.backfill_platform.nfo_expiry_rotation import _select_strike_window, _target_expiries
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker

logger = logging.getLogger(__name__)

# Kite's NFO "name" -> the index's NSE tradingsymbol (quote and history).
UNDERLYINGS = {"NIFTY": "NIFTY 50"}
EXPIRIES = 4
STRIKE_WINDOW = 40
CAPTURE_WINDOW = 60
FIRST_MARK = time(9, 0)
LAST_MARK = time(15, 30)
MARK_STEP = timedelta(minutes=15)
MARKS_PER_SESSION = (LAST_MARK.hour * 60 + LAST_MARK.minute - FIRST_MARK.hour * 60 - FIRST_MARK.minute) // 15 + 1  # 27
PRE_OPEN_END = time(9, 15)
# A live quote this long after the mark still counts as the mark's OI.
CAPTURE_GRACE = timedelta(seconds=120)
LATE_AFTER = timedelta(seconds=60)
QUOTE_BATCH = 500
QUOTE_PAUSE_SECONDS = 1.1  # Kite: 1 quote request a second
HISTORY_PAUSE_SECONDS = 0.4  # Kite: 3 history requests a second, shared with the backfill
# Only marks at least this old are gap-filled: the candle closing at the
# mark needs a moment to appear in Kite's history.
FILL_DELAY = timedelta(minutes=5)
FILL_LOOKBACK_SESSIONS = 5
LOW_COVERAGE = 0.9
# ΔOI (both sides, absolute) under this share of total OI reads as flat.
FLAT_FRACTION = 0.001
CALC_VERSION = 1

# Snapshot writes and the "changed since" pass run one at a time: a gap
# filled while a live record lands would otherwise leave the live one
# compared with the wrong predecessor.
write_lock = asyncio.Lock()


# ---------------------------------------------------------------- marks


def session_marks(d: date) -> list[datetime]:
    """The 27 marks of one session, 09:00-15:30 IST, as aware UTC."""
    marks = []
    t = datetime.combine(d, FIRST_MARK, tzinfo=IST)
    last = datetime.combine(d, LAST_MARK, tzinfo=IST)
    while t <= last:
        marks.append(t.astimezone(timezone.utc))
        t += MARK_STEP
    return marks


def ist_session(ts: datetime) -> date:
    return as_aware_utc(ts).astimezone(IST).date()


def latest_mark(now: datetime) -> datetime | None:
    """The most recent mark at or before `now` (walking back over non-
    trading days), or None if there isn't one within a few weeks."""
    now = as_aware_utc(now)
    d = ist_session(now)
    for _ in range(30):
        if is_trading_day(d):
            marks = [m for m in session_marks(d) if m <= now]
            if marks:
                return marks[-1]
        d -= timedelta(days=1)
    return None


def due_capture_mark(now: datetime) -> datetime | None:
    """The mark a live capture is due for right now, if any: one that
    passed no more than CAPTURE_GRACE ago."""
    mark = latest_mark(now)
    if mark is not None and as_aware_utc(now) - mark <= CAPTURE_GRACE:
        return mark
    return None


def expected_marks(until: datetime, since: datetime | None = None, limit: int | None = None) -> list[datetime]:
    """Marks up to `until`, newest first -- back to `since` (inclusive) or
    `limit` of them, whichever comes first."""
    out: list[datetime] = []
    until = as_aware_utc(until)
    since = as_aware_utc(since) if since is not None else None
    d = ist_session(until)
    for _ in range(4000):
        if is_trading_day(d):
            for m in reversed(session_marks(d)):
                if m > until:
                    continue
                if since is not None and m < since:
                    return out
                out.append(m)
                if limit is not None and len(out) >= limit:
                    return out
        if since is None and limit is None:
            break
        d -= timedelta(days=1)
    return out


# ---------------------------------------------------------------- chain


@dataclass(frozen=True)
class Contract:
    expiry: date
    strike: float
    option_type: str
    tradingsymbol: str
    instrument_token: str


def chain_for(nfo_rows: list[dict], kite_name: str, session: date) -> dict[date, list[Contract]]:
    """The next EXPIRIES expiries on/after `session` from Kite's NFO dump,
    each with every listed CE/PE contract."""
    expiries = _target_expiries(nfo_rows, kite_name, session, EXPIRIES)
    wanted = {e.isoformat(): e for e in expiries}
    chain: dict[date, list[Contract]] = {e: [] for e in expiries}
    for row in nfo_rows:
        if row.get("name") != kite_name:
            continue
        option_type = (row.get("instrument_type") or "").upper()
        if option_type not in ("CE", "PE"):
            continue
        expiry = wanted.get(str(row.get("expiry")))
        if expiry is None or row.get("strike") in (None, "", "0"):
            continue
        chain[expiry].append(Contract(expiry, float(row["strike"]), option_type, row["tradingsymbol"], str(row["instrument_token"])))
    return chain


def capture_contracts(chain: dict[date, list[Contract]], spot: float) -> list[Contract]:
    """ATM ±CAPTURE_WINDOW strikes of each expiry, CE and PE."""
    out: list[Contract] = []
    for contracts in chain.values():
        window = set(_select_strike_window([c.strike for c in contracts], spot, CAPTURE_WINDOW))
        out.extend(c for c in contracts if c.strike in window)
    return out


def nearest_strike(strikes: list[float], spot: float) -> float | None:
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else None


def strike_step(strikes: list[float], atm: float | None) -> float | None:
    ordered = sorted(set(strikes))
    if atm is None or len(ordered) < 2:
        return None
    i = ordered.index(atm)
    gaps = [ordered[j + 1] - ordered[j] for j in (i - 1, i) if 0 <= j < len(ordered) - 1]
    return min(gaps) if gaps else None


# ---------------------------------------------------------------- derive


def classify(put_change: float | None, call_change: float | None, spot_change: float | None, total_oi: float | None):
    """(positioning, oi_driver) from the two ΔOI and the NIFTY move.

    Put build-up larger than call build-up, or calls unwinding, leans
    bullish; the mirror image leans bearish; a lean the NIFTY move
    disagrees with is a divergence. OI can't tell writers from buyers --
    this reads it the usual way for index options, as mostly written."""
    if put_change is None or call_change is None:
        return None, None
    if put_change < 0 and call_change < 0:
        return "unwinding", "both_unwinding"
    if total_oi and abs(put_change) + abs(call_change) < FLAT_FRACTION * total_oi:
        return "flat", "flat"
    net = put_change - call_change
    if net > 0:
        if call_change < 0:
            driver = "put_buildup_call_unwinding" if put_change > 0 else "call_unwinding"
        else:
            driver = "put_led_buildup"
        return ("bullish" if spot_change is None or spot_change >= 0 else "divergence"), driver
    if net < 0:
        if put_change < 0:
            driver = "call_buildup_put_unwinding" if call_change > 0 else "put_unwinding"
        else:
            driver = "call_led_buildup"
        return ("bearish" if spot_change is None or spot_change <= 0 else "divergence"), driver
    return "flat", "flat"


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


Key = tuple[date, float, str]


async def _strike_map(db: AsyncSession, snapshot_id, cache: dict) -> dict[Key, float | None]:
    if snapshot_id not in cache:
        rows = (
            await db.execute(
                select(PcrStrikeOi.expiry, PcrStrikeOi.strike, PcrStrikeOi.option_type, PcrStrikeOi.oi)
                .where(PcrStrikeOi.snapshot_id == snapshot_id)
            )
        ).all()
        cache[snapshot_id] = {(e, s, t): oi for e, s, t, oi in rows}
    return cache[snapshot_id]


async def derive(db: AsyncSession, snap: PcrSnapshot, cache: dict | None = None) -> None:
    """Totals over ATM ±STRIKE_WINDOW, per expiry and summed, and every
    "changed since" field, from the stored contract rows. Idempotent --
    the gap fill re-runs it for records whose predecessor it just filled."""
    cache = {} if cache is None else cache
    now_map = await _strike_map(db, snap.id, cache)
    prev = (
        await db.execute(
            select(PcrSnapshot).where(PcrSnapshot.underlying == snap.underlying, PcrSnapshot.ts < snap.ts)
            .order_by(PcrSnapshot.ts.desc()).limit(1)
        )
    ).scalar_one_or_none()
    base = (
        await db.execute(
            select(PcrSnapshot).where(PcrSnapshot.underlying == snap.underlying, PcrSnapshot.session_date < snap.session_date)
            .order_by(PcrSnapshot.ts.desc()).limit(1)
        )
    ).scalar_one_or_none()
    prev_map = await _strike_map(db, prev.id, cache) if prev is not None else {}
    base_map = await _strike_map(db, base.id, cache) if base is not None else {}

    by_expiry: dict[date, list[Key]] = defaultdict(list)
    for key in now_map:
        by_expiry[key[0]].append(key)

    spot = snap.spot
    expiry_rows: list[PcrSnapshotExpiry] = []
    tot = defaultdict(float)
    have_prev = prev is not None
    have_base = base is not None
    compared = 0
    expected = with_oi = 0
    record_atm = record_step = None
    for expiry in sorted(by_expiry):
        keys = by_expiry[expiry]
        strikes = sorted({k[1] for k in keys})
        window = set(_select_strike_window(strikes, spot, STRIKE_WINDOW)) if spot is not None else set()
        atm = nearest_strike(strikes, spot) if spot is not None else None
        if record_atm is None:
            record_atm, record_step = atm, strike_step(strikes, atm)
        e = defaultdict(float)
        e_expected = e_with_oi = 0
        for key in keys:
            if key[1] not in window:
                continue
            side = "call" if key[2] == "CE" else "put"
            e_expected += 1
            oi = now_map[key]
            if oi is None:
                continue
            e_with_oi += 1
            e[side] += oi
            if prev_map.get(key) is not None:
                e[f"{side}_chg"] += oi - prev_map[key]
                compared += 1
            if base_map.get(key) is not None:
                e[f"{side}_day"] += oi - base_map[key]
        expected += e_expected
        with_oi += e_with_oi
        for k, v in e.items():
            tot[k] += v
        expiry_rows.append(PcrSnapshotExpiry(
            expiry=expiry, atm_strike=atm,
            strike_lo=min(window) if window else None, strike_hi=max(window) if window else None,
            contracts_expected=e_expected, contracts_with_oi=e_with_oi,
            total_call_oi=e["call"] if e_with_oi else None, total_put_oi=e["put"] if e_with_oi else None,
            pcr=_ratio(e["put"], e["call"]) if e_with_oi else None,
            call_oi_change=e["call_chg"] if have_prev else None, put_oi_change=e["put_chg"] if have_prev else None,
            oi_change_pcr=_ratio(e["put_chg"], e["call_chg"]) if have_prev else None,
            call_oi_change_day=e["call_day"] if have_base else None, put_oi_change_day=e["put_day"] if have_base else None,
        ))

    snap.strike_window = STRIKE_WINDOW
    snap.atm_strike = record_atm
    snap.strike_step = record_step
    snap.contracts_expected = expected
    snap.contracts_with_oi = with_oi
    snap.total_call_oi = tot["call"] if with_oi else None
    snap.total_put_oi = tot["put"] if with_oi else None
    snap.pcr = _ratio(tot["put"], tot["call"]) if with_oi else None

    snap.prev_ts = prev.ts if have_prev else None
    snap.prev_pcr = prev.pcr if have_prev else None
    snap.pcr_change = (snap.pcr - prev.pcr) if have_prev and snap.pcr is not None and prev.pcr is not None else None
    snap.spot_change = (spot - prev.spot) if have_prev and spot is not None and prev.spot is not None else None
    snap.spot_change_pct = (snap.spot_change / prev.spot * 100) if snap.spot_change is not None and prev.spot else None
    snap.atm_shift = (record_atm - prev.atm_strike) if have_prev and record_atm is not None and prev.atm_strike is not None else None
    snap.call_oi_change = tot["call_chg"] if have_prev else None
    snap.put_oi_change = tot["put_chg"] if have_prev else None
    snap.oi_change_pcr = _ratio(tot["put_chg"], tot["call_chg"]) if have_prev else None
    snap.oi_change_contracts = compared if have_prev else None
    snap.day_baseline_ts = base.ts if have_base else None
    snap.call_oi_change_day = tot["call_day"] if have_base else None
    snap.put_oi_change_day = tot["put_day"] if have_base else None
    snap.oi_change_pcr_day = _ratio(tot["put_day"], tot["call_day"]) if have_base else None
    total_oi = (snap.total_call_oi or 0) + (snap.total_put_oi or 0)
    snap.positioning, snap.oi_driver = classify(snap.put_oi_change, snap.call_oi_change, snap.spot_change, total_oi)

    flags = []
    if as_aware_utc(snap.ts).astimezone(IST).time() < PRE_OPEN_END:
        flags.append("pre_open")
    if snap.source == SOURCE_LIVE and as_aware_utc(snap.captured_at) - as_aware_utc(snap.ts) > LATE_AFTER:
        flags.append("late")
    if have_prev:
        expected_prev = expected_marks(as_aware_utc(snap.ts) - timedelta(seconds=1), limit=1)
        if not expected_prev or as_aware_utc(prev.ts) != expected_prev[0]:
            flags.append("gap_before")
        if set(prev.expiries or []) != set(snap.expiries or []):
            flags.append("expiry_rolled")
    if expected and with_oi < LOW_COVERAGE * expected:
        flags.append("low_coverage")
    snap.flags = flags
    snap.calc_version = CALC_VERSION

    snap.expiry_rows.clear()
    await db.flush()
    snap.expiry_rows.extend(expiry_rows)


async def _load(db: AsyncSession, snapshot_id) -> PcrSnapshot:
    return (
        await db.execute(
            select(PcrSnapshot).where(PcrSnapshot.id == snapshot_id)
            .options(selectinload(PcrSnapshot.expiry_rows)).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def rederive_from(db: AsyncSession, underlying: str, from_ts: datetime) -> int:
    """Re-runs derive() for every record at/after `from_ts`, oldest first
    (each compares with the one before it)."""
    ids = (
        await db.execute(
            select(PcrSnapshot.id).where(PcrSnapshot.underlying == underlying, PcrSnapshot.ts >= from_ts).order_by(PcrSnapshot.ts)
        )
    ).scalars().all()
    cache: dict = {}
    for snapshot_id in ids:
        await derive(db, await _load(db, snapshot_id), cache)
    return len(ids)


async def _write(
    db: AsyncSession, underlying: str, mark: datetime, source: str, captured_at: datetime, spot: float | None,
    expiries: list[date], contracts: list[Contract], values: dict[str, tuple[float | None, float | None, float | None]],
) -> PcrSnapshot:
    snap = PcrSnapshot(
        underlying=underlying, ts=mark, session_date=ist_session(mark), captured_at=captured_at, source=source,
        strike_window=STRIKE_WINDOW, expiries=[e.isoformat() for e in expiries], spot=spot,
        contracts_expected=0, contracts_with_oi=0, flags=[], calc_version=CALC_VERSION,
    )
    db.add(snap)
    await db.flush()
    # Core insert: a multi-session gap fill writes ~1,000 rows per record,
    # too many to hold as ORM objects until the commit.
    rows = []
    for c in contracts:
        oi, price, volume = values.get(c.tradingsymbol, (None, None, None))
        rows.append({
            "id": uuid.uuid4(), "snapshot_id": snap.id, "expiry": c.expiry, "strike": c.strike,
            "option_type": c.option_type, "tradingsymbol": c.tradingsymbol, "oi": oi, "last_price": price, "volume": volume,
        })
    if rows:
        await db.execute(insert(PcrStrikeOi), rows)
    snap = await _load(db, snap.id)
    await derive(db, snap)
    return snap


async def snapshot_exists(db: AsyncSession, underlying: str, mark: datetime) -> bool:
    return (
        await db.execute(select(PcrSnapshot.id).where(PcrSnapshot.underlying == underlying, PcrSnapshot.ts == mark))
    ).first() is not None


# ---------------------------------------------------------------- live


def _num(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


async def capture_live(db: AsyncSession, broker: ZerodhaKiteBroker, underlying: str, mark: datetime) -> PcrSnapshot | None:
    """Captures `mark` from Kite quotes. None if a record for it already
    exists. Raises KiteAPIError when Kite can't be read."""
    if await snapshot_exists(db, underlying, mark):
        return None
    index = f"NSE:{UNDERLYINGS[underlying]}"
    nfo_rows = await broker.get_instruments("NFO")
    chain = chain_for(nfo_rows, underlying, ist_session(mark))
    if len(chain) < EXPIRIES:
        raise KiteAPIError(f"Only {len(chain)} {underlying} expiries in Kite's instrument list")
    spot = (await broker.get_ltp("NSE", UNDERLYINGS[underlying]))["price"]
    contracts = capture_contracts(chain, spot)
    values: dict[str, tuple[float | None, float | None, float | None]] = {}
    for i in range(0, len(contracts), QUOTE_BATCH):
        await asyncio.sleep(QUOTE_PAUSE_SECONDS)
        batch = contracts[i : i + QUOTE_BATCH]
        quotes = await broker.get_quote_batch([f"NFO:{c.tradingsymbol}" for c in batch])
        for c in batch:
            q = quotes.get(f"NFO:{c.tradingsymbol}")
            if q:
                values[c.tradingsymbol] = (_num(q.get("oi")), _num(q.get("last_price")), _num(q.get("volume")))
    captured_at = datetime.now(timezone.utc)
    logger.debug("PCR capture %s %s: %d contracts, index %s", underlying, mark, len(contracts), index)
    async with write_lock:
        if await snapshot_exists(db, underlying, mark):
            return None
        snap = await _write(db, underlying, mark, SOURCE_LIVE, captured_at, spot, list(chain), contracts, values)
        await db.commit()
    return snap


# ---------------------------------------------------------------- read


SNAPSHOT_COLUMNS = (
    "source", "captured_at", "expiries", "spot", "atm_strike", "strike_step", "strike_window",
    "contracts_expected", "contracts_with_oi", "total_call_oi", "total_put_oi", "pcr",
    "prev_ts", "prev_pcr", "pcr_change", "spot_change", "spot_change_pct", "atm_shift",
    "call_oi_change", "put_oi_change", "oi_change_pcr", "oi_change_contracts",
    "day_baseline_ts", "call_oi_change_day", "put_oi_change_day", "oi_change_pcr_day",
    "positioning", "oi_driver", "flags",
)
EXPIRY_COLUMNS = (
    "expiry", "atm_strike", "strike_lo", "strike_hi", "contracts_expected", "contracts_with_oi",
    "total_call_oi", "total_put_oi", "pcr", "call_oi_change", "put_oi_change", "oi_change_pcr",
    "call_oi_change_day", "put_oi_change_day",
)
MAX_ROWS = 3000


async def snapshot_rows(
    db: AsyncSession, underlying: str, now: datetime, limit: int = 25,
    start: date | None = None, end: date | None = None, include_expiries: bool = False,
) -> list[dict]:
    """Every mark in the requested span, newest first -- the latest `limit`,
    or the sessions `start`..`end` -- one row each: the record, or a
    "missing"/"pending" row where there is none, so the sequence stays
    continuous. Starts no earlier than the first record ever made."""
    first = (
        await db.execute(select(PcrSnapshot.ts).where(PcrSnapshot.underlying == underlying).order_by(PcrSnapshot.ts).limit(1))
    ).scalar_one_or_none()
    last = latest_mark(now)
    if first is None or last is None:
        return []
    first = as_aware_utc(first)
    if start is not None or end is not None:
        until = min(last, session_marks(end)[-1]) if end is not None else last
        since = max(first, session_marks(start)[0]) if start is not None else first
        marks = expected_marks(until, since=since, limit=MAX_ROWS)
    else:
        marks = expected_marks(last, since=first, limit=min(limit, MAX_ROWS))
    if not marks:
        return []

    query = select(PcrSnapshot).where(
        PcrSnapshot.underlying == underlying, PcrSnapshot.ts >= marks[-1], PcrSnapshot.ts <= marks[0],
    )
    if include_expiries:
        query = query.options(selectinload(PcrSnapshot.expiry_rows))
    by_ts = {as_aware_utc(s.ts): s for s in (await db.execute(query)).scalars().all()}

    rows = []
    for mark in marks:
        snap = by_ts.get(mark)
        row = {"ts": mark, "session_date": ist_session(mark)}
        if snap is None:
            row["status"] = "pending" if as_aware_utc(now) - mark <= CAPTURE_GRACE else "missing"
        else:
            row["status"] = "recorded"
            row.update({col: getattr(snap, col) for col in SNAPSHOT_COLUMNS})
            if include_expiries:
                row["expiry_rows"] = [{col: getattr(e, col) for col in EXPIRY_COLUMNS} for e in snap.expiry_rows]
        rows.append(row)
    return rows


async def latest_pcr(db: AsyncSession, underlying: str, as_of: datetime) -> float | None:
    """PCR of the latest record at/before `as_of` with enough coverage to
    trust -- None when there's none yet."""
    snaps = (
        await db.execute(
            select(PcrSnapshot).where(PcrSnapshot.underlying == underlying, PcrSnapshot.ts <= as_of, PcrSnapshot.pcr.is_not(None))
            .order_by(PcrSnapshot.ts.desc()).limit(5)
        )
    ).scalars().all()
    for snap in snaps:
        if snap.contracts_expected and snap.contracts_with_oi >= LOW_COVERAGE * snap.contracts_expected:
            return snap.pcr
    return None


# ---------------------------------------------------------------- gap fill


async def missing_marks(db: AsyncSession, underlying: str, now: datetime, sessions: int = FILL_LOOKBACK_SESSIONS) -> list[datetime]:
    """Marks of the last `sessions` trading sessions, old enough to fill,
    with no record -- oldest first."""
    until = as_aware_utc(now) - FILL_DELAY
    last = latest_mark(until)
    if last is None:
        return []
    first_day = ist_session(last)
    for _ in range(sessions - 1):
        first_day = previous_trading_day(first_day)
    marks = expected_marks(last, since=session_marks(first_day)[0])
    have = set(
        as_aware_utc(ts) for ts in (
            await db.execute(
                select(PcrSnapshot.ts).where(
                    PcrSnapshot.underlying == underlying, PcrSnapshot.ts >= marks[-1], PcrSnapshot.ts <= marks[0],
                )
            )
        ).scalars().all()
    ) if marks else set()
    return sorted(m for m in marks if m not in have)


async def _known_expiries(db: AsyncSession, underlying: str) -> set[date]:
    """Every expiry this app has seen for the underlying, expired ones
    included (Kite's dump lists only live contracts)."""
    bf = (
        await db.execute(
            select(BfSymbol.expiry).where(BfSymbol.source == "zerodha_nfo", BfSymbol.underlying_symbol == underlying, BfSymbol.expiry.is_not(None)).distinct()
        )
    ).scalars().all()
    idx = (await db.execute(select(Instrument.id).where(Instrument.symbol == UNDERLYINGS[underlying]))).scalar_one_or_none()
    inst = []
    if idx is not None:
        inst = (
            await db.execute(
                select(Instrument.expiry).where(Instrument.underlying_instrument_id == idx, Instrument.expiry.is_not(None)).distinct()
            )
        ).scalars().all()
    snaps = (await db.execute(select(PcrSnapshot.expiries).where(PcrSnapshot.underlying == underlying))).scalars().all()
    seen = {date.fromisoformat(e) for row in snaps for e in (row or [])}
    return set(bf) | set(inst) | seen


def fillable(session: date, today: date, known_expiries: set[date]) -> bool:
    """All of the session's expiries are still listed: none has passed
    between the session and today."""
    return not any(session <= e < today for e in known_expiries)


def _oi_at(candles: list[dict], mark: datetime) -> tuple[float | None, float | None]:
    """OI and price of the last candle closed by `mark` (candles sorted,
    `ts` = candle start)."""
    cutoff = mark - MARK_STEP
    last = None
    for c in candles:
        if as_aware_utc(c["ts"]) <= cutoff:
            last = c
        else:
            break
    if last is None:
        return None, None
    return _num(last.get("open_interest")), _num(last.get("close"))


def _spot_at(candles: list[dict], mark: datetime) -> float | None:
    """NIFTY at `mark`: the close of the candle ending at it; at 09:15 the
    session's first open; at 09:00 the previous close."""
    mark_ist = as_aware_utc(mark).astimezone(IST)
    if mark_ist.time() == PRE_OPEN_END:
        for c in candles:
            if as_aware_utc(c["ts"]) == as_aware_utc(mark):
                return _num(c.get("open"))
    _, close = _oi_at(candles, mark)
    return close


async def fill_gaps(
    db: AsyncSession, broker: ZerodhaKiteBroker | None, underlying: str, now: datetime, sessions: int = FILL_LOOKBACK_SESSIONS,
) -> dict:
    """Fills every missing mark of the last `sessions` sessions it can.
    Returns {"filled": n, "unfillable": m, "missing": k}, plus
    "waiting_login" when there is something to fill but no Kite session."""
    missing = await missing_marks(db, underlying, now, sessions)
    if not missing:
        return {"filled": 0, "unfillable": 0, "missing": 0}
    today = ist_session(now)
    known = await _known_expiries(db, underlying)
    todo = [m for m in missing if fillable(ist_session(m), today, known)]
    result = {"filled": 0, "unfillable": len(missing) - len(todo), "missing": len(missing)}
    if not todo:
        return result
    if broker is None:
        return {**result, "waiting_login": True}

    nfo_rows = await broker.get_instruments("NFO")
    start = datetime.combine(previous_trading_day(ist_session(todo[0])), FIRST_MARK, tzinfo=IST)
    end = as_aware_utc(now)
    index_candles = await broker.get_historical_data(UNDERLYINGS[underlying], "15m", start, end, segment="NSE")
    index_candles.sort(key=lambda c: as_aware_utc(c["ts"]))

    plans: list[tuple[datetime, float, list[date], list[Contract]]] = []
    wanted: dict[str, Contract] = {}
    for mark in todo:
        spot = _spot_at(index_candles, mark)
        chain = chain_for(nfo_rows, underlying, ist_session(mark))
        if spot is None or len(chain) < EXPIRIES:
            result["unfillable"] += 1
            continue
        contracts = capture_contracts(chain, spot)
        plans.append((mark, spot, list(chain), contracts))
        for c in contracts:
            wanted[c.tradingsymbol] = c
    if not plans:
        return result

    history: dict[str, list[dict]] = {}
    for c in wanted.values():
        history[c.tradingsymbol] = await _contract_history(broker, c, start, end)

    captured_at = datetime.now(timezone.utc)
    for mark, spot, expiries, contracts in plans:
        async with write_lock:
            if await snapshot_exists(db, underlying, mark):
                continue
            values = {}
            for c in contracts:
                oi, price = _oi_at(history.get(c.tradingsymbol, []), mark)
                values[c.tradingsymbol] = (oi, price, None)
            await _write(db, underlying, mark, SOURCE_HISTORICAL, captured_at, spot, expiries, contracts, values)
            await db.commit()
        result["filled"] += 1
    # Records after a filled one were compared with an older predecessor.
    async with write_lock:
        await rederive_from(db, underlying, plans[0][0])
        await db.commit()
    return result


async def _contract_history(broker: ZerodhaKiteBroker, c: Contract, start: datetime, end: datetime) -> list[dict]:
    """15-minute candles with OI, oldest first. Retried twice (the history
    rate limit is shared with the backfill queue); a contract that still
    fails aborts the pass rather than being saved as missing -- the next
    pass starts over."""
    for attempt in range(3):
        await asyncio.sleep(HISTORY_PAUSE_SECONDS if attempt == 0 else 3.0 * attempt)
        try:
            candles = await broker.get_historical_data(
                c.tradingsymbol, "15m", start, end, segment="NFO", instrument_token=c.instrument_token,
            )
            return sorted(candles, key=lambda x: as_aware_utc(x["ts"]))
        except KiteAPIError as exc:
            if "TokenException" in str(exc) or attempt == 2:
                raise
    return []
