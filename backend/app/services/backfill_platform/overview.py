"""What the Data Backfill page, the Dashboard card and the top-bar pill
show: per segment and timeframe how far the data is saved and how many
sessions behind it is, the job queue, what needs attention, the schedule.

Everything comes from bf_coverage (one row per symbol and timeframe) and
the jobs/runs tables -- never an aggregate over the 20M stored bars -- and
is cached for a few seconds, since the top-bar pill asks on every page.
"""

import shutil
import time as time_module
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.backfill_platform import BfBackfillJob, BfBackfillRun, BfBackfillStatus, BfCoverage, BfSymbol
from app.models.broker import Broker, BrokerAccount, BrokerConnection, ConnectionStatus
from app.models.instrument import Instrument
from app.services.backfill_platform.coverage import (
    IST,
    KITE_SOURCES,
    NSE_FULL_DAY_BARS,
    get_settings,
    is_trading_day,
    ist_date,
    last_completed_session,
    next_trading_day,
    nse_bar_end,
    session_close,
    sessions_behind,
)
from app.services.backfill_platform.jobs import INTERRUPTED_PREFIX, unresolved_failures
from app.services.backfill_platform.topup import (
    RUN_RUNNING, RUN_WAITING_LOGIN, _topup_time, enabled_sources, keeps_candles, untraded_active_contracts,
)
from app.services.backfill_platform.worker import backfill_worker
from app.services.market_data.active_timeframe_sync_scheduler import active_timeframe_sync_scheduler
from app.services.market_data.bar_periods import BAR_DURATIONS

SEGMENTS = {"zerodha": "NSE Equity", "zerodha_nfo": "NFO Options & Futures", "delta": "Delta Exchange"}
UNIT = {"zerodha": "stocks", "zerodha_nfo": "contracts", "delta": "symbols"}
TIMEFRAMES = ["5m", "15m", "30m", "60m", "1d"]  # no 1-minute (timeframes.py)
TIMEFRAME_LABEL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m", "1d": "Daily"}
ATTENTION_DAYS = 7
_CACHE_SECONDS = 15
_cache: dict[str, tuple[float, object]] = {}


def clear_cache() -> None:
    _cache.clear()


async def _cached(key: str, build):
    hit = _cache.get(key)
    if hit and time_module.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = await build()
    _cache[key] = (time_module.monotonic(), value)
    return value


def status_for(behind: int) -> str:
    return "ok" if behind == 0 else "warn" if behind == 1 else "bad"


@dataclass
class Pair:
    symbol_id: object
    symbol: str
    source: str
    timeframe: str
    first_ts: datetime
    last_ts: datetime
    bar_count: int
    last_day_bars: int | None
    expired: bool
    behind: int
    partial: bool

    @property
    def saved_up_to(self) -> datetime:
        return nse_bar_end(self.last_ts, self.timeframe) if self.source in KITE_SOURCES else as_aware_utc(self.last_ts) + BAR_DURATIONS.get(self.timeframe, timedelta(0))


async def _pairs(db: AsyncSession, now: datetime, sources: tuple[str, ...]) -> list[Pair]:
    rows = (
        await db.execute(
            select(BfCoverage, BfSymbol.symbol, BfSymbol.source, BfSymbol.expiry)
            .join(BfSymbol, BfSymbol.id == BfCoverage.symbol_id)
            .where(BfSymbol.source.in_(sources))
        )
    ).all()
    out = []
    for cov, symbol, source, expiry in rows:
        last_day = ist_date(cov.last_ts)
        expired = expiry is not None and expiry <= last_day
        kite = source in KITE_SOURCES
        behind = 0 if (expired or not kite) else sessions_behind(cov.last_ts, cov.timeframe, now, cov.checked_through)
        full = NSE_FULL_DAY_BARS.get(cov.timeframe)
        partial = source == "zerodha" and full is not None and cov.last_day_bars is not None and cov.last_day_bars < full and behind == 0 \
            and last_day == last_completed_session(now)
        out.append(Pair(cov.symbol_id, symbol, source, cov.timeframe, as_aware_utc(cov.first_ts), as_aware_utc(cov.last_ts),
                        cov.bar_count, cov.last_day_bars, expired, behind, partial))
    return out


def _cell(pairs: list[Pair], timeframe: str, updating: bool) -> dict:
    if not pairs:
        return {"timeframe": timeframe, "status": "none", "symbols": 0}
    active = [p for p in pairs if not p.expired]
    # Only expired contracts: their history is closed, nothing is missing.
    basis = active or pairs
    behind = [p for p in active if p.behind > 0]
    worst = max((p.behind for p in active), default=0)
    return {
        "timeframe": timeframe,
        "status": status_for(worst),
        "updating": updating,
        "saved_up_to": max(p.saved_up_to for p in basis),
        "oldest_saved_up_to": min(p.saved_up_to for p in basis),
        "sessions_behind": worst,
        "symbols": len(active),
        "expired": len(pairs) - len(active),
        "current": len(active) - len(behind),
        "behind": len(behind),
        "partial": sum(p.partial for p in active),
        "bars": sum(p.bar_count for p in pairs),
        "history_from": min(p.first_ts for p in pairs),
    }


async def _updating_pairs(db: AsyncSession) -> set[tuple[str, str]]:
    rows = (
        await db.execute(
            select(BfBackfillJob.source, BfBackfillJob.timeframe)
            .where(BfBackfillJob.status.in_([BfBackfillStatus.PENDING.value, BfBackfillStatus.RUNNING.value]))
            .distinct()
        )
    ).all()
    return {(s, tf) for s, tf in rows}


async def _zerodha_login(db: AsyncSession) -> dict:
    """Whether a Zerodha account is logged in -- the session monitor flips a
    connection to "error" once Kite's daily token (~06:00 IST) has expired."""
    statuses = set(
        (
            await db.execute(
                select(BrokerConnection.status)
                .join(BrokerAccount, BrokerAccount.id == BrokerConnection.broker_account_id)
                .join(Broker, Broker.id == BrokerAccount.broker_id)
                .where(Broker.code == "zerodha_kite")
            )
        ).scalars()
    )
    if ConnectionStatus.CONNECTED.value in statuses:
        return {"connected": True, "status": "connected"}
    return {"connected": False, "status": "expired" if statuses else "not_set_up"}


def _next_run_at(settings, now: datetime) -> datetime | None:
    if not enabled_sources(settings):
        return None
    at = _topup_time(settings.topup_time)
    day = as_aware_utc(now).astimezone(IST).date()
    if not (is_trading_day(day) and as_aware_utc(now) < datetime.combine(day, at, tzinfo=IST)):
        day = next_trading_day(day)
    return datetime.combine(day, at, tzinfo=IST)


def _live_today(now: datetime, nse_ids: set) -> datetime | None:
    """How far today's candles have come in through the chart sync (market hours)."""
    today = as_aware_utc(now).astimezone(IST).date()
    latest = None
    for (instrument_id, timeframe), ts in list(active_timeframe_sync_scheduler._newest_fetched.items()):
        if timeframe not in ("5m", "15m") or instrument_id not in nse_ids:
            continue
        ts = as_aware_utc(ts)
        if ist_date(ts) != today:
            continue
        end = min(ts + BAR_DURATIONS[timeframe], session_close(today), as_aware_utc(now))
        latest = end if latest is None or end > latest else latest
    return latest


async def _nse_instrument_ids(db: AsyncSession) -> set:
    keys = [k[0] for k in active_timeframe_sync_scheduler._newest_fetched]
    if not keys:
        return set()
    return set((await db.execute(select(Instrument.id).where(Instrument.id.in_(keys), Instrument.exchange == "NSE"))).scalars())


async def _queue(db: AsyncSession, now: datetime) -> dict:
    runs = (await db.execute(select(BfBackfillRun).where(BfBackfillRun.status.in_([RUN_RUNNING, RUN_WAITING_LOGIN])))).scalars().all()
    running_runs = [r for r in runs if r.status == RUN_RUNNING]
    counts: Counter = Counter()
    if running_runs:
        for status, n in (
            await db.execute(
                select(BfBackfillJob.status, func.count()).where(BfBackfillJob.run_id.in_([r.id for r in running_runs])).group_by(BfBackfillJob.status)
            )
        ).all():
            counts[status] = n
    queued_total = (
        await db.execute(select(func.count()).where(BfBackfillJob.status == BfBackfillStatus.PENDING.value))
    ).scalar_one()
    current = None
    if backfill_worker.current_job_id is not None:
        job = await db.get(BfBackfillJob, backfill_worker.current_job_id)
        if job is not None:
            symbol = await db.get(BfSymbol, job.symbol_id)
            current = {"symbol": symbol.symbol if symbol else None, "timeframe": job.timeframe, "source": job.source, "from_date": job.start_date}
    total = sum(r.jobs_total for r in running_runs)
    done = counts.get(BfBackfillStatus.COMPLETED.value, 0)
    failed = counts.get(BfBackfillStatus.FAILED.value, 0)
    started = min((r.started_at for r in running_runs if r.started_at), default=None)
    finished = done + failed + counts.get(BfBackfillStatus.CANCELLED.value, 0)
    eta = None
    if started and finished and total > finished:
        elapsed = (as_aware_utc(now) - as_aware_utc(started)).total_seconds()
        eta = int(elapsed / finished * (total - finished))
    last = (
        await db.execute(select(BfBackfillRun).where(BfBackfillRun.completed_at.is_not(None)).order_by(BfBackfillRun.completed_at.desc()).limit(1))
    ).scalars().first()
    waiting = next((r for r in runs if r.status == RUN_WAITING_LOGIN), None)
    return {
        "state": "paused" if backfill_worker.paused else "running" if (running_runs or queued_total) else "waiting_login" if waiting else "idle",
        "run": {
            "kind": running_runs[0].kind, "started_at": started, "total": total, "done": done, "failed": failed,
            "running": counts.get(BfBackfillStatus.RUNNING.value, 0), "queued": counts.get(BfBackfillStatus.PENDING.value, 0),
            "percent": round(100 * finished / total) if total else 0, "eta_seconds": eta,
        } if running_runs else None,
        "queued_total": queued_total,
        "current": current,
        "waiting_message": waiting.message if waiting else None,
        "last_run": {
            "kind": last.kind, "source": last.source, "status": last.status, "completed_at": last.completed_at,
            "message": last.message, "session_date": last.session_date,
        } if last else None,
    }


async def _failed_groups(db: AsyncSession, now: datetime) -> list[dict]:
    interrupted = BfBackfillJob.error_message.like(INTERRUPTED_PREFIX + "%")
    rows = (
        await db.execute(
            select(BfBackfillJob.source, BfBackfillJob.timeframe, interrupted.label("interrupted"), func.count(), func.max(BfBackfillJob.completed_at))
            .where(*unresolved_failures(now, ATTENTION_DAYS))
            .group_by(BfBackfillJob.source, BfBackfillJob.timeframe, interrupted)
        )
    ).all()
    return [{"source": s, "timeframe": tf, "interrupted": bool(i), "count": n, "last": last} for s, tf, i, n, last in rows]


def _fmt_day(d: datetime | date) -> str:
    if isinstance(d, datetime):
        d = as_aware_utc(d).astimezone(IST)
    return f"{d:%a} {d.day} {d:%b}"


async def _attention(db: AsyncSession, now: datetime, segments: list[dict], login: dict, settings) -> list[dict]:
    items: list[dict] = []
    if not login["connected"]:
        items.append({"severity": "bad", "title": "Zerodha is not logged in",
                      "detail": "Automatic top-ups wait for a login -- Settings > Brokers.", "action": {"type": "link", "href": "/settings"}})
    groups = await _failed_groups(db, now)
    interrupted = [g for g in groups if g["interrupted"]]
    other = [g for g in groups if not g["interrupted"]]
    for kind, gs, title in (("interrupted", interrupted, "stopped by a server restart"), ("failed", other, "failed")):
        n = sum(g["count"] for g in gs)
        if not n:
            continue
        top = max(gs, key=lambda g: g["count"])
        where = ", ".join(sorted({f"{SEGMENTS.get(g['source'], g['source']).split()[0]} {TIMEFRAME_LABEL.get(g['timeframe'], g['timeframe'])}" for g in gs}))
        last = max(g["last"] for g in gs if g["last"])
        items.append({"severity": "bad", "title": f"{n:,} {'job' if n == 1 else 'jobs'} {title}",
                      "detail": f"{where} · last {_fmt_day(last)} {as_aware_utc(last).astimezone(IST):%H:%M}",
                      "action": {"type": "retry_failed", "kind": kind, "count": n}, "_order": top["count"]})
    for seg in segments:
        if seg["source"] not in KITE_SOURCES:
            continue
        for cell in seg["cells"]:
            if cell["status"] in ("ok", "none"):
                continue
            n = cell["behind"]
            unit = UNIT[seg["source"]] if n != 1 else UNIT[seg["source"]][:-1]
            name = SEGMENTS[seg["source"]].split()[0]
            tf = TIMEFRAME_LABEL[cell["timeframe"]]
            s = cell["sessions_behind"]
            if n == cell["symbols"]:
                title = f"{name} {tf} is {s} session{'s' if s != 1 else ''} behind"
            else:
                title = f"{n:,} {name} {unit} behind on {tf}"
            items.append({"severity": cell["status"], "title": title,
                          "detail": f"{n:,} {unit} · oldest saved up to {_fmt_day(cell['oldest_saved_up_to'])}",
                          "action": {"type": "topup", "source": seg["source"]}})
    session = last_completed_session(now)
    empty_total = (
        await db.execute(
            select(func.count()).select_from(BfSymbol)
            .where(BfSymbol.source == "zerodha_nfo", ~exists().where(BfCoverage.symbol_id == BfSymbol.id), keeps_candles())
        )
    ).scalar_one()
    if empty_total:
        active = (await db.execute(select(func.count()).select_from(untraded_active_contracts(session).subquery()))).scalar_one()
        expired = empty_total - active
        detail = []
        if active:
            detail.append(f"{active:,} still active -- retried in each NFO top-up")
        if expired:
            detail.append(f"{expired:,} expired -- Kite keeps no history for expired contracts")
        items.append({"severity": "info", "title": f"{empty_total:,} NFO contract{'s have' if empty_total != 1 else ' has'} no data yet",
                      "detail": "; ".join(detail) + ".", "action": None})
    if settings.coverage_built_at is None:
        items.insert(0, {"severity": "info", "title": "Counting what is saved",
                         "detail": "First run after the update -- coverage appears within a few minutes.", "action": None})
    rank = {"bad": 0, "warn": 1, "info": 2}
    items.sort(key=lambda i: rank.get(i["severity"], 3))
    for i in items:
        i.pop("_order", None)
    return items


async def _storage(db: AsyncSession) -> dict | None:
    if db.get_bind().dialect.name != "postgresql":
        return None
    size = (await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
    tables = {
        name: {"bytes": b, "rows": int(r)}
        for name, b, r in (
            await db.execute(text(
                "SELECT c.relname, pg_total_relation_size(c.oid), c.reltuples FROM pg_class c "
                "WHERE c.relname IN ('bf_ohlcv_bars', 'ohlcv_candles') AND c.relkind = 'r'"
            ))
        ).all()
    }
    disk = shutil.disk_usage("/")
    return {"database_bytes": size, "tables": tables, "disk_used_bytes": disk.used, "disk_total_bytes": disk.total, "backups_verified": False}


def _headline(cells: list[dict], session: date) -> dict:
    tracked = [c for c in cells if c["status"] != "none"]
    if not tracked:
        return {"saved_up_to": None, "status": "none", "behind": [], "timeframes": 0, "timeframes_current": 0}
    ok = [c for c in tracked if c["status"] == "ok"]
    worst = max(c["sessions_behind"] for c in tracked)
    return {
        "saved_up_to": min(c["saved_up_to"] for c in ok) if ok else min(c["saved_up_to"] for c in tracked),
        "status": status_for(worst),
        "last_session": session,
        "timeframes": len(tracked),
        "timeframes_current": len(ok),
        "behind": [{"timeframe": c["timeframe"], "sessions_behind": c["sessions_behind"]} for c in tracked if c["status"] != "ok"],
    }


async def build_overview(db: AsyncSession) -> dict:
    async def build():
        now = datetime.now(timezone.utc)
        settings = await get_settings(db)
        session = last_completed_session(now)
        pairs = await _pairs(db, now, ("zerodha", "zerodha_nfo", "delta"))
        updating = await _updating_pairs(db)
        segments = []
        for source in ("zerodha", "zerodha_nfo"):
            mine = [p for p in pairs if p.source == source]
            symbols = len({p.symbol_id for p in mine if not p.expired})
            segments.append({
                "source": source, "label": SEGMENTS[source], "unit": UNIT[source], "symbols": symbols,
                "enabled": getattr(settings, "auto_topup_" + source),
                "cells": [_cell([p for p in mine if p.timeframe == tf], tf, (source, tf) in updating) for tf in TIMEFRAMES],
            })
        delta = [p for p in pairs if p.source == "delta"]
        segments.append({
            "source": "delta", "label": SEGMENTS["delta"], "unit": UNIT["delta"], "paused": not settings.delta_enabled,
            "symbols": len({p.symbol_id for p in delta}), "last_saved_at": max((p.last_ts for p in delta), default=None), "cells": [],
        })
        login = await _zerodha_login(db)
        live = _live_today(now, await _nse_instrument_ids(db))
        return {
            "as_of": now,
            "coverage_ready": settings.coverage_built_at is not None,
            "last_session": session,
            "headline": _headline(segments[0]["cells"], session),
            "live_today_until": live,
            "zerodha_login": login,
            "segments": segments,
            "queue": await _queue(db, now),
            "attention": await _attention(db, now, segments, login, settings),
            "schedule": schedule_out(settings, now),
            "storage": await _storage(db),
        }

    return await _cached("overview", build)


def schedule_out(settings, now: datetime) -> dict:
    return {
        "auto_topup_zerodha": settings.auto_topup_zerodha,
        "auto_topup_zerodha_nfo": settings.auto_topup_zerodha_nfo,
        "delta_enabled": settings.delta_enabled,
        "topup_time": settings.topup_time,
        "live_start": settings.live_start,
        "live_end": settings.live_end,
        "topup_timeframes": list(settings.topup_timeframes),
        "next_run_at": _next_run_at(settings, now),
        "worker_paused": backfill_worker.paused,
    }


async def build_stocks(db: AsyncSession, source: str, status_filter: str, q: str, offset: int, limit: int) -> dict:
    async def build():
        now = datetime.now(timezone.utc)
        pairs = await _pairs(db, now, (source,))
        updating = await _updating_pairs(db)
        failed = set(
            (
                await db.execute(
                    select(BfBackfillJob.symbol_id, BfBackfillJob.timeframe)
                    .where(BfBackfillJob.source == source, *unresolved_failures(now, ATTENTION_DAYS))
                )
            ).all()
        )
        failed_symbols = {sid for sid, _tf in failed}
        by_symbol: dict = {}
        for p in pairs:
            by_symbol.setdefault((p.symbol_id, p.symbol), []).append(p)
        rows = []
        for (sid, name), ps in by_symbol.items():
            cells = {}
            for p in ps:
                cells[p.timeframe] = {
                    "saved_up_to": p.saved_up_to, "sessions_behind": p.behind,
                    "status": "updating" if (source, p.timeframe) in updating and p.behind > 0 else ("expired" if p.expired else status_for(p.behind)),
                    "partial": p.partial, "failed": (sid, p.timeframe) in failed, "bars": p.bar_count,
                }
            active = [p for p in ps if not p.expired]
            rows.append({
                "symbol_id": str(sid), "symbol": name, "cells": cells,
                "history_from": min(p.first_ts for p in ps),
                "expired": not active,
                "behind": any(p.behind > 0 for p in active),
                "partial": any(p.partial for p in active),
                "failed": sid in failed_symbols,
            })
        rows.sort(key=lambda r: r["symbol"])
        return rows

    rows = await _cached(f"stocks:{source}", build)
    counts = {
        "all": len(rows),
        "current": sum(1 for r in rows if not r["behind"] and not r["expired"]),
        "behind": sum(1 for r in rows if r["behind"]),
        "partial": sum(1 for r in rows if r["partial"]),
        "failed": sum(1 for r in rows if r["failed"]),
    }
    pick = {
        "all": lambda r: True, "current": lambda r: not r["behind"] and not r["expired"],
        "behind": lambda r: r["behind"], "partial": lambda r: r["partial"], "failed": lambda r: r["failed"],
    }.get(status_filter, lambda r: True)
    q = q.strip().upper()
    filtered = [r for r in rows if pick(r) and (not q or q in r["symbol"].upper())]
    return {"counts": counts, "total": len(filtered), "rows": filtered[offset : offset + limit]}
