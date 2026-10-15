"""NIFTY's top-3 OTM open interest at each 15-minute PCR record, pushed to
Telegram -- where option writers are stacked: the three calls above NIFTY
with the most OI (resistance) and the three puts below it with the most OI
(support), each with its OI and how that changed since the previous record
and since the previous session's last one.

Read from the record's own contract rows (pcr_snapshots.py: ATM ±60 strikes
of the next 4 expiries, quoted at the mark), so it costs no Kite calls. The
nearest expiry; on its expiry day the next one as well, since the expiring
contracts' OI unwinds through the afternoon. Sent for live records from
09:15 (09:00 is the pre-open, still yesterday's OI) every
Settings.oi_levels_alert_minutes; a gap-filled record is history, not news.
"""

from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import as_aware_utc
from app.models.pcr import PcrSnapshot
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram
from app.services.options.pcr_snapshots import Key, _strike_map

TOP_N = 3
FIRST_ALERT = time(9, 15)
LAKH = 100_000.0


@dataclass
class Level:
    strike: float
    oi: float
    change: float | None  # since the previous record
    change_day: float | None  # since the previous session's last record


def alert_due(mark: datetime, every_minutes: int) -> bool:
    """Marks from 09:15 IST, every `every_minutes` (0 or less: never)."""
    if every_minutes <= 0:
        return False
    t = as_aware_utc(mark).astimezone(IST)
    since_first = (t.hour * 60 + t.minute) - (FIRST_ALERT.hour * 60 + FIRST_ALERT.minute)
    return since_first >= 0 and since_first % every_minutes == 0


def _diff(now: float, then: float | None) -> float | None:
    return None if then is None else now - then


def top_levels(
    oi_map: dict[Key, float | None], expiry: date, spot: float, option_type: str,
    prev_map: dict[Key, float | None] | None = None, base_map: dict[Key, float | None] | None = None, n: int = TOP_N,
) -> list[Level]:
    """The `n` out-of-the-money contracts of `expiry` with the most OI --
    calls above `spot`, puts below it; the nearer strike first on a tie."""
    prev_map, base_map = prev_map or {}, base_map or {}
    above = option_type == "CE"
    picks = [
        (strike, oi) for (e, strike, t), oi in oi_map.items()
        if e == expiry and t == option_type and oi is not None and (strike > spot if above else strike < spot)
    ]
    picks.sort(key=lambda p: (-p[1], abs(p[0] - spot)))
    return [
        Level(strike, oi, _diff(oi, prev_map.get((expiry, strike, option_type))), _diff(oi, base_map.get((expiry, strike, option_type))))
        for strike, oi in picks[:n]
    ]


def _lakh(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value / LAKH:+.1f}" if signed else f"{value / LAKH:.1f}"


def _table(title: str, levels: list[Level], spot: float, change_label: str) -> list[str]:
    lines = [title, f"#  {'Strike':>6} {'OI(L)':>6} {change_label:>6} {'ΔDay':>6} {'Away':>5}"]
    for i, lvl in enumerate(levels, start=1):
        lines.append(
            f"{i}  {lvl.strike:>6,.0f} {_lakh(lvl.oi):>6} {_lakh(lvl.change, True):>6} {_lakh(lvl.change_day, True):>6} "
            f"{lvl.strike - spot:>+5.0f}"
        )
    if not levels:
        lines.append("   none in the captured strikes")
    return lines


def _strike(levels: list[Level]) -> str:
    return f"{levels[0].strike:,.0f}" if levels else "—"


def format_message(
    snap: PcrSnapshot, sections: list[tuple[date, list[Level], list[Level], float | None, float | None]],
    change_label: str,
) -> tuple[str, str]:
    """(subject, body) for send_telegram -- the body goes out as a
    monospace block, so columns line up. `sections`: per expiry shown,
    (expiry, resistance, support, previous record's #1 resistance strike,
    previous #1 support strike)."""
    mark = as_aware_utc(snap.ts).astimezone(IST)
    _, first_res, first_sup, _, _ = sections[0]
    subject = f"NIFTY OI levels {mark:%H:%M} · S {_strike(first_sup)} / R {_strike(first_res)}"

    head = f"NIFTY {snap.spot:,.1f}"
    if snap.spot_change is not None:
        head += f" ({snap.spot_change:+.1f})"
    if snap.pcr is not None:
        head += f"  PCR {snap.pcr:.2f}"
    lines = [head]
    for expiry, res, sup, prev_res, prev_sup in sections:
        lines.append("")
        lines.append(f"Expiry {expiry:%d %b}" + (" (today)" if expiry == mark.date() else ""))
        lines += _table("RESISTANCE: call OI above NIFTY", res, snap.spot, change_label)
        lines += _table("SUPPORT: put OI below NIFTY", sup, snap.spot, change_label)
        if res and sup:
            lines.append(f"Range {sup[0].strike:,.0f} – {res[0].strike:,.0f}")
        if res and prev_res is not None and prev_res != res[0].strike:
            lines.append(f"Shift: resistance {prev_res:,.0f} → {res[0].strike:,.0f}")
        if sup and prev_sup is not None and prev_sup != sup[0].strike:
            lines.append(f"Shift: support {prev_sup:,.0f} → {sup[0].strike:,.0f}")
    lines.append("")
    lines.append("OI(L) lakh · Away = pts from NIFTY")
    lines.append("ΔDay = since previous close")
    return subject, "\n".join(lines)


async def _record_at(db: AsyncSession, underlying: str, ts: datetime | None) -> PcrSnapshot | None:
    if ts is None:
        return None
    return (
        await db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == underlying, PcrSnapshot.ts == ts))
    ).scalar_one_or_none()


async def oi_levels_message(db: AsyncSession, snap: PcrSnapshot) -> tuple[str, str] | None:
    """(subject, body) of the OI-levels alert for this record, or None when
    it has no NIFTY level or expiries to read."""
    if snap.spot is None or not snap.expiries:
        return None
    cache: dict = {}
    now_map = await _strike_map(db, snap.id, cache)
    prev = await _record_at(db, snap.underlying, snap.prev_ts)
    base = await _record_at(db, snap.underlying, snap.day_baseline_ts)
    prev_map = await _strike_map(db, prev.id, cache) if prev is not None else {}
    base_map = await _strike_map(db, base.id, cache) if base is not None else {}

    mark = as_aware_utc(snap.ts).astimezone(IST)
    expiries = [date.fromisoformat(e) for e in snap.expiries]
    shown = expiries[:2] if expiries[0] == mark.date() and len(expiries) > 1 else expiries[:1]
    sections = []
    for expiry in shown:
        res = top_levels(now_map, expiry, snap.spot, "CE", prev_map, base_map)
        sup = top_levels(now_map, expiry, snap.spot, "PE", prev_map, base_map)
        prev_res = prev_sup = None
        if prev is not None and prev.spot is not None:
            prev_res = next((lvl.strike for lvl in top_levels(prev_map, expiry, prev.spot, "CE", n=1)), None)
            prev_sup = next((lvl.strike for lvl in top_levels(prev_map, expiry, prev.spot, "PE", n=1)), None)
        sections.append((expiry, res, sup, prev_res, prev_sup))

    if prev is None:
        change_label = "Δprev"
    else:
        minutes = round((as_aware_utc(snap.ts) - as_aware_utc(prev.ts)).total_seconds() / 60)
        change_label = f"Δ{minutes}m" if minutes < 1000 else "Δprev"
    return format_message(snap, sections, change_label)


async def send_oi_levels(db: AsyncSession, snap: PcrSnapshot) -> bool:
    """Sends this live record's OI-levels alert when one is due. True if a
    message went to send_telegram (which itself no-ops when Telegram isn't
    set up)."""
    if not alert_due(snap.ts, get_settings().oi_levels_alert_minutes):
        return False
    message = await oi_levels_message(db, snap)
    if message is None:
        return False
    await send_telegram(*message)
    return True
