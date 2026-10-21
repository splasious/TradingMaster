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

Sent as a picture (21 Oct, oi_levels_chart.py: each expiry a price ladder)
with a short summary under it -- the levels, NIFTY and PCR, any level that
moved. A picture that can't be drawn sends the full table as text instead.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import as_aware_utc
from app.models.pcr import PcrSnapshot
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram
from app.services.options import oi_levels_chart
from app.services.options.oi_levels_chart import Bar, OiChart, Panel
from app.services.options.pcr_snapshots import Key, _strike_map

logger = logging.getLogger(__name__)

TOP_N = 3
# Characters per line of the monospace block. In a group Telegram shows the
# sender's photo beside each message, so the block is narrower than in the
# private chat: 36-character rows wrapped there on a phone (15 Oct).
MAX_WIDTH = 32
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
    """`levels` most OI first; listed lowest strike first (15 Oct), each
    with its OI rank in the # column."""
    lines = [title, f"# {'Strike':>6} {'OI(L)':>5} {change_label:>5} {'ΔDay':>6} {'Away':>4}"]
    for rank, lvl in sorted(enumerate(levels, start=1), key=lambda ranked: ranked[1].strike):
        lines.append(
            f"{rank} {lvl.strike:>6,.0f} {_lakh(lvl.oi):>5} {_lakh(lvl.change, True):>5} {_lakh(lvl.change_day, True):>6} "
            f"{lvl.strike - spot:>+4.0f}"
        )
    if not levels:
        lines.append("   none in the captured strikes")
    return lines


def _strike(levels: list[Level]) -> str:
    return f"{levels[0].strike:,.0f}" if levels else "—"


Section = tuple[date, list[Level], list[Level], float | None, float | None]


def _subject(snap: PcrSnapshot, sections: list[Section]) -> str:
    mark = as_aware_utc(snap.ts).astimezone(IST)
    _, first_res, first_sup, _, _ = sections[0]
    return f"NIFTY OI levels {mark:%H:%M} · S {_strike(first_sup)} / R {_strike(first_res)}"


def _head(snap: PcrSnapshot) -> str:
    head = f"NIFTY {snap.spot:,.1f}"
    if snap.spot_change is not None:
        head += f" ({snap.spot_change:+.1f})"
    if snap.pcr is not None:
        head += f" PCR {snap.pcr:.2f}"
    return head


def format_message(snap: PcrSnapshot, sections: list[Section], change_label: str) -> tuple[str, str]:
    """(subject, body) for send_telegram -- the body goes out as a
    monospace block, so columns line up. `sections`: per expiry shown,
    (expiry, resistance, support, previous record's #1 resistance strike,
    previous #1 support strike)."""
    mark = as_aware_utc(snap.ts).astimezone(IST)
    subject = _subject(snap, sections)
    lines = [_head(snap)]
    for expiry, res, sup, prev_res, prev_sup in sections:
        lines.append("")
        lines.append(f"Expiry {expiry:%d %b}" + (" (today)" if expiry == mark.date() else ""))
        lines += _table("RESISTANCE: call OI above NIFTY", res, snap.spot, change_label)
        lines += _table("SUPPORT: put OI below NIFTY", sup, snap.spot, change_label)
        if res and sup:
            lines.append(f"Range {sup[0].strike:,.0f} – {res[0].strike:,.0f}")
        if res and prev_res is not None and prev_res != res[0].strike:
            lines.append(f"Shift R: {prev_res:,.0f} → {res[0].strike:,.0f}")
        if sup and prev_sup is not None and prev_sup != sup[0].strike:
            lines.append(f"Shift S: {prev_sup:,.0f} → {sup[0].strike:,.0f}")
    lines.append("")
    lines.append("OI(L) in lakh · Away in points")
    lines.append("ΔDay = since previous close")
    return subject, "\n".join(lines)


async def _record_at(db: AsyncSession, underlying: str, ts: datetime | None) -> PcrSnapshot | None:
    if ts is None:
        return None
    return (
        await db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == underlying, PcrSnapshot.ts == ts))
    ).scalar_one_or_none()


def caption(snap: PcrSnapshot, sections: list[Section]) -> tuple[str, str]:
    """(subject, body) under the picture: the levels per expiry and any #1
    strike that moved since the previous record."""
    lines = [_head(snap)]
    for expiry, res, sup, _, _ in sections:
        lines.append(f"{expiry:%d %b}  S {_strike(sup)} · R {_strike(res)}")
    for expiry, res, sup, prev_res, prev_sup in sections:
        if sup and prev_sup is not None and prev_sup != sup[0].strike:
            lines.append(f"{expiry:%d %b}  S moved {prev_sup:,.0f}→{sup[0].strike:,.0f}")
        if res and prev_res is not None and prev_res != res[0].strike:
            lines.append(f"{expiry:%d %b}  R moved {prev_res:,.0f}→{res[0].strike:,.0f}")
    return _subject(snap, sections), "\n".join(lines)


def _bars(levels: list[Level]) -> list[Bar]:
    def lakh(value: float | None) -> float | None:
        return None if value is None else value / LAKH

    return [Bar(rank, lvl.strike, lvl.oi / LAKH, lakh(lvl.change), lakh(lvl.change_day)) for rank, lvl in enumerate(levels, start=1)]


def _moved(name: str, before: float | None, levels: list[Level]) -> str | None:
    if not levels or before is None or before == levels[0].strike:
        return None
    up = levels[0].strike > before
    return f"{'▲' if up else '▼'} {name} moved {'up' if up else 'down'}  {before:,.0f} → {levels[0].strike:,.0f}"


def chart_of(snap: PcrSnapshot, sections: list[Section], change_label: str) -> OiChart:
    mark = as_aware_utc(snap.ts).astimezone(IST)
    panels = []
    for expiry, res, sup, prev_res, prev_sup in sections:
        notes = [f"Range {sup[0].strike:,.0f} – {res[0].strike:,.0f}"] if res and sup else []
        notes += [note for note in (_moved("Support", prev_sup, sup), _moved("Resistance", prev_res, res)) if note]
        panels.append(Panel(f"Expiry {expiry:%d %b}" + (" · today" if expiry == mark.date() else ""), _bars(res), _bars(sup), notes))
    expiry, res, sup, _, _ = sections[0]
    spot = snap.spot
    parts = []
    if res:
        parts.append(f"{res[0].strike - spot:,.0f} pts below resistance {res[0].strike:,.0f}")
    if sup:
        parts.append(f"{spot - sup[0].strike:,.0f} pts above support {sup[0].strike:,.0f}")
    summary = f"{expiry:%d %b}: NIFTY {spot:,.0f} is " + " and ".join(parts) + "." if parts else f"NIFTY {spot:,.0f}"
    return OiChart(mark, spot, snap.spot_change, snap.pcr, panels, change_label.removeprefix("Δ"), summary)


async def _sections(db: AsyncSession, snap: PcrSnapshot) -> tuple[list[Section], str] | None:
    """Per expiry shown, its levels (see format_message), and what the
    changes are against ("Δ15m"); None when the record has no NIFTY level
    or expiries to read."""
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
    return sections, change_label


async def oi_levels_message(db: AsyncSession, snap: PcrSnapshot) -> tuple[str, str] | None:
    """(subject, body) of the OI-levels alert as text, or None when the
    record has no NIFTY level or expiries to read."""
    built = await _sections(db, snap)
    return format_message(snap, *built) if built is not None else None


async def _picture(snap: PcrSnapshot, sections: list[Section], change_label: str) -> bytes | None:
    try:
        return await asyncio.to_thread(oi_levels_chart.draw, chart_of(snap, sections, change_label))
    except Exception:
        logger.exception("OI levels picture not drawn; the alert goes as text")
        return None


async def send_oi_levels(db: AsyncSession, snap: PcrSnapshot) -> bool:
    """Sends this live record's OI-levels alert when one is due: the picture
    with a short summary, or the full table as text when the picture can't
    be drawn. True if a message went to send_telegram (which itself no-ops
    when Telegram isn't set up)."""
    if not alert_due(snap.ts, get_settings().oi_levels_alert_minutes):
        return False
    built = await _sections(db, snap)
    if built is None:
        return False
    sections, change_label = built
    photo = await _picture(snap, sections, change_label)
    if photo is None:
        await send_telegram(*format_message(snap, sections, change_label), to_group=True)
    else:
        await send_telegram(*caption(snap, sections), to_group=True, photo=photo)
    return True
