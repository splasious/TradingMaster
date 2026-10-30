"""MACD - RSI - 15 MIN's live run (30 Oct): why it hasn't traded.

Read-only. Today's live run(s) of the strategy -- status, capital, the PCR
part of its last reason and how many stocks each other part names, its
state, its orders today -- the NIFTY PCR records today (the filter: out
below 0.80 until back over 0.90), and every fresh MACD up-cross on the
watchlist's 15-minute candles since the run started -- the only buys it
makes -- with each one's price against the run's per-slot money
(capital / MAX_POSITIONS: a stock dearer than that buys 0 shares,
skipped without a word).

Run inside the backend container (the health check's `macd_live_check`
input pipes it to `python -`). Stock names and prices go to the PRIVATE
Telegram chat only; the public Actions log gets counts, times and the PCR.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.audit import AuditLog
from app.models.instrument import Instrument
from app.models.live_native import LiveNativeDeployment, LiveNativePosition
from app.models.live_trading import LiveOrder
from app.models.paper_trading import PaperNativeDeployment
from app.models.pcr import PcrSnapshot
from app.models.strategy import Strategy
from app.services.broker.zerodha_broker import IST
from app.services.market_data.bar_periods import load_closed_candles
from app.services.notifications.telegram import send_telegram
from app.services.strategy.native_strategies import macd_rsi_15min as m

NAME = "MACD - RSI - 15 MIN"


def up_crosses(bars: list[dict], since: datetime) -> list[tuple[datetime, float, float | None]]:
    """(close time, close, RSI) of every candle closing from `since` on that
    the strategy saw as a fresh up-cross: compute_signal over the HISTORY_BARS
    candles it reads, ending with that one."""
    out = []
    for i in range(len(bars)):
        closed_at = as_aware_utc(bars[i]["ts"]) + m.BAR_LENGTH
        if closed_at < since:
            continue
        sig = m.compute_signal(bars[max(0, i + 1 - m.HISTORY_BARS):i + 1])
        if sig is not None and sig["buy"]:
            out.append((closed_at, float(bars[i]["close"]), sig["rsi"]))
    return out


def parts(reason: str | None) -> tuple[str, list[str]]:
    """The reason's PCR part as is, and each other part as its label and how many it names."""
    bits = (reason or "").split(" | ")
    counted = []
    for bit in bits[1:]:
        label, _, names = bit.partition(": ")
        counted.append(f"{label}: {len([n for n in names.split(', ') if n])}" if names else label)
    return bits[0], counted


async def main() -> None:
    now = datetime.now(timezone.utc)
    today = now.astimezone(IST).date()
    log, private = [], []
    async with AsyncSessionLocal() as db:
        runs = (
            await db.execute(
                select(LiveNativeDeployment).join(Strategy, Strategy.id == LiveNativeDeployment.strategy_id)
                .where(func.trim(Strategy.name) == NAME).order_by(LiveNativeDeployment.created_at)
            )
        ).scalars().all()
        runs = [r for r in runs if as_aware_utc(r.created_at).astimezone(IST).date() == today or r.status == "active"]
        for r in runs:
            pcr_part, counted = parts(r.last_signal_reason)
            held = (await db.execute(select(func.count()).select_from(LiveNativePosition).where(LiveNativePosition.deployment_id == r.id))).scalar()
            orders = (
                await db.execute(
                    select(LiveOrder.status, func.count()).where(LiveOrder.native_deployment_id == r.id,
                                                                 LiveOrder.created_at >= datetime.combine(today, datetime.min.time(), tzinfo=IST))
                    .group_by(LiveOrder.status)
                )
            ).all()
            state = r.state or {}
            log.append(
                f"live run {str(r.id)[:8]}: {r.status}, started {as_aware_utc(r.created_at).astimezone(IST):%d-%b %H:%M}, "
                f"last run {as_aware_utc(r.last_evaluated_at).astimezone(IST):%H:%M:%S}" if r.last_evaluated_at else f"live run {str(r.id)[:8]}: never run"
            )
            log.append(f"  capital Rs {r.capital:,.0f} -> Rs {r.capital / m.MAX_POSITIONS:,.0f} a slot; product {r.product_style}; "
                       f"paused with a reason: {'yes' if r.pause_reason else 'no'}; positions at the broker {held}; "
                       f"in its state {len(state.get('holdings') or {})}; PCR filter out: {bool(state.get('pcr_risk_off'))}")
            log.append(f"  last reason: {pcr_part} | {' | '.join(counted)}")
            log.append(f"  orders today: {', '.join(f'{s} x{n}' for s, n in orders) or 'none'}")
            paper = await db.get(PaperNativeDeployment, r.paper_deployment_id) if r.paper_deployment_id else None
            paper_held = set(((paper.state or {}).get("holdings") or {}) if paper else {})
            live_held = set(state.get("holdings") or {})
            copy_audit = (
                await db.execute(
                    select(AuditLog.new_value).where(AuditLog.object_id == str(r.id), AuditLog.action == "LIVE_NATIVE_VERSION_UPDATED")
                    .order_by(AuditLog.created_at.desc()).limit(1)
                )
            ).scalar()
            copies = (copy_audit or {}).get("copies") or []
            log.append(f"  copy: {len(copies)} queued from the paper card at the switch; still waiting {len(state.get('adopt') or [])}; "
                       f"paper card holds {len(paper_held)} now, {len(paper_held & live_held)} of them held live too, "
                       f"{len(set(copies) - live_held)} queued but not held live")
            private.append(f"Copy for live run {str(r.id)[:8]}: queued {', '.join(copies) or '-'}; waiting {', '.join(state.get('adopt') or []) or '-'}; "
                           f"paper holds {', '.join(sorted(paper_held)) or '-'}; live holds {', '.join(sorted(live_held)) or '-'}")
            private.append(f"Live run {str(r.id)[:8]} ({r.status}) last reason:\n{r.last_signal_reason or '-'}")

        start = min((as_aware_utc(r.created_at) for r in runs), default=now - timedelta(hours=8))
        slot = min((r.capital for r in runs), default=50_000.0) / m.MAX_POSITIONS

        records = (
            await db.execute(
                select(PcrSnapshot.ts, PcrSnapshot.pcr).where(PcrSnapshot.underlying == "NIFTY", PcrSnapshot.session_date == today)
                .order_by(PcrSnapshot.ts)
            )
        ).all()
        readings = [(as_aware_utc(ts).astimezone(IST), p) for ts, p in records if p is not None]
        log.append("NIFTY PCR today: " + (", ".join(f"{t:%H:%M} {p:.2f}" for t, p in readings) or "no records"))

        universe = (
            await db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(m.WATCHLIST)))
        ).scalars().all()
        crosses, newest, dear = [], [], 0
        for instrument in universe:
            bars = await load_closed_candles(db, instrument.id, m.TIMEFRAME, m.HISTORY_BARS + 40, now)
            if bars:
                newest.append(as_aware_utc(bars[-1]["ts"]) + m.BAR_LENGTH)
                dear += bars[-1]["close"] > slot
            crosses += [(t, instrument.symbol, close, rsi) for t, close, rsi in up_crosses(bars, start - m.BAR_LENGTH)]
        crosses.sort()
        stale = sum(1 for t in newest if now - t > timedelta(minutes=35))
        log.append(f"watchlist: {len(universe)} of {len(m.WATCHLIST)} found; newest 15m candle closed "
                   f"{max(newest).astimezone(IST):%H:%M} (stocks over 35 min behind: {stale}); "
                   f"{dear} priced above Rs {slot:,.0f} a slot (0 shares)" if newest else "watchlist: no 15m candles")
        affordable = [c for c in crosses if c[2] <= slot]
        log.append(f"fresh MACD up-crosses since the run started: {len(crosses)} "
                   f"({', '.join(f'{t.astimezone(IST):%H:%M}' for t, *_ in crosses) or '-'}); affordable at Rs {slot:,.0f} a slot: {len(affordable)}")
        private.append("Fresh MACD up-crosses since the live run started (close time, stock, close, RSI, fits a slot of "
                       f"Rs {slot:,.0f}):\n" + ("\n".join(
                           f"{t.astimezone(IST):%H:%M} {s} {c:,.2f} RSI {r:.1f} {'yes' if c <= slot else 'NO -- 0 shares'}" if r is not None
                           else f"{t.astimezone(IST):%H:%M} {s} {c:,.2f}" for t, s, c, r in crosses) or "none"))
    await send_telegram("MACD - RSI - 15 MIN · live run check", "\n\n".join(private))
    print("\n".join(log))


if __name__ == "__main__":
    asyncio.run(main())
