"""The PCR Analysis tab's live NIFTY 50 chart on the server (3 Nov): does it get
what it needs? Read-only -- no order, no Kite call, nothing saved.

Asks the running app, as a page does: GET /options/oi-chart (candles, the
latest record, the nearest expiry's contracts and their instrument ids),
then the market-data WebSocket for NIFTY and every contract for a few
seconds -- how many come back with a real (Kite) price and live OI -- and
ranks the top three each side from those ticks as the page does. Plus the
live runs' status, so a restart that upset one shows here.

Run inside the backend container (the health check's `oi_chart_check`
input pipes it to `python -`). The public Actions log gets counts, times,
yes/no and NIFTY market data only (strikes, OI in lakh) -- no names, no
user, no token.
"""

import asyncio
import json
import time

import httpx
import websockets
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.core.time import as_aware_utc
from app.models.audit import AuditLog
from app.db.session import AsyncSessionLocal
from app.models.live_native import LiveNativeDeployment, LiveNativePosition
from app.models.strategy import Strategy
from app.models.user import User
from app.services.broker.zerodha_broker import IST

BASE = "http://localhost:8000"
LISTEN_SECONDS = 8


def ist(ts: str | None) -> str:
    from datetime import datetime

    return datetime.fromisoformat(ts).astimezone(IST).strftime("%d-%b %H:%M") if ts else "-"


def top(contracts: list[dict], oi: dict[str, float], spot: float, side: str) -> list[tuple[float, float]]:
    rows = []
    for c in contracts:
        value = oi.get(c["instrument_id"]) if c["instrument_id"] else None
        value = value if value is not None else c["oi"]
        if c["option_type"] == side and value is not None and (c["strike"] > spot if side == "CE" else c["strike"] < spot):
            rows.append((c["strike"], value))
    rows.sort(key=lambda r: (-r[1], abs(r[0] - spot)))
    return rows[:3]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.is_active.is_(True), User.is_approved.is_(True)).limit(1))
        ).scalars().first()
        runs = (await db.execute(select(LiveNativeDeployment.status, func.count()).group_by(LiveNativeDeployment.status))).all()
        paused = (await db.execute(
            select(func.count()).select_from(LiveNativeDeployment).where(LiveNativeDeployment.pause_reason.is_not(None),
                                                                          LiveNativeDeployment.status == "paused")
        )).scalar()
        print(f"live runs by status: {', '.join(f'{s} {n}' for s, n in runs) or 'none'}; paused with a reason: {paused}")
        # Each run: when it started, stopped and last ran; the Stop button's audit record (no user shown).
        for run, name in (await db.execute(
            select(LiveNativeDeployment, Strategy.name).join(Strategy, Strategy.id == LiveNativeDeployment.strategy_id)
            .order_by(LiveNativeDeployment.created_at)
        )).all():
            audits = (await db.execute(
                select(AuditLog.action, AuditLog.created_at, AuditLog.new_value).where(AuditLog.object_id == str(run.id))
                .order_by(AuditLog.created_at)
            )).all()
            stop = next(((t, v) for a, t, v in reversed(audits) if a == "LIVE_NATIVE_STOPPED"), None)
            held = (await db.execute(select(func.count()).select_from(LiveNativePosition).where(LiveNativePosition.deployment_id == run.id))).scalar()
            when = lambda t: as_aware_utc(t).astimezone(IST).strftime("%d-%b %H:%M:%S") if t else "-"  # noqa: E731
            print(f"  run {str(run.id)[:8]} ({name.strip()}): {run.status}; started {when(run.created_at)}; stopped {when(run.stopped_at)}; "
                  f"last ran {when(run.last_evaluated_at)}; positions held {held}; Stop-button record: "
                  + (f"{when(stop[0])}, closed positions: {(stop[1] or {}).get('closed_positions')}, "
                     f"left in the account: {len((stop[1] or {}).get('left_in_account') or [])}" if stop else "none"))
            print(f"    audit actions: {', '.join(f'{a} {when(t)}' for a, t, _ in audits[-6:]) or '-'}")
    if user is None:
        print("no active user to ask the app as")
        return
    token = create_access_token(str(user.id), [])

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        response = await client.get("/api/v1/options/oi-chart?underlying=NIFTY", headers={"Authorization": f"Bearer {token}"})
    print(f"GET /options/oi-chart: HTTP {response.status_code}")
    if response.status_code != 200:
        return
    data = response.json()
    contracts = data["contracts"]
    with_id = [c for c in contracts if c["instrument_id"]]
    print(f"market open: {data['market_open']}; NIFTY instrument: {'yes' if data['nifty_instrument_id'] else 'no'}; "
          f"live price in the tick engine: {'yes' if data['live_price'] else 'no'}")
    print(f"candles: {len(data['candles'])} finished, newest starts {ist(data['candles'][-1]['ts']) if data['candles'] else '-'}; "
          f"forming: {ist(data['forming']['ts']) if data['forming'] else 'none'}")
    print(f"record: {ist(data['record_ts'])} (previous {ist(data['prev_ts'])}), NIFTY {data['record_spot']}, PCR {data['record_pcr']}; "
          f"expiry {data['expiry']} of {len(data['expiries'])}")
    print(f"contracts: {len(contracts)}; with an instrument (live OI possible): {len(with_id)}; "
          f"with record OI: {sum(1 for c in contracts if c['oi'] is not None)}; previous: {sum(1 for c in contracts if c['prev_oi'] is not None)}; "
          f"previous close: {sum(1 for c in contracts if c['day_oi'] is not None)}")

    ids = ([data["nifty_instrument_id"]] if data["nifty_instrument_id"] else []) + [c["instrument_id"] for c in with_id]
    ticks: dict[str, dict] = {}
    counts: dict[str, int] = {}
    async with websockets.connect(f"ws://localhost:8000/api/v1/ws/market-data?token={token}", max_size=2**22) as ws:
        await ws.send(json.dumps({"type": "subscribe", "instrument_ids": ids}))
        deadline = time.monotonic() + LISTEN_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
            except asyncio.TimeoutError:
                break
            if message.get("type") == "tick":
                ticks[message["instrument_id"]] = message
                counts[message["instrument_id"]] = counts.get(message["instrument_id"], 0) + 1
    nifty = ticks.get(data["nifty_instrument_id"] or "")
    kite_oi = {i: t["open_interest"] for i, t in ticks.items() if t.get("source") == "kite" and t.get("open_interest") is not None}
    print(f"websocket, {LISTEN_SECONDS}s: ticks for {len(ticks)} of {len(ids)} instruments, {sum(counts.values())} in all; "
          f"NIFTY tick: {nifty['source'] + ' ' + str(nifty['price']) if nifty else 'none'}; contracts with live Kite OI: {len(kite_oi)} of {len(with_id)}")
    record_oi = {c["instrument_id"]: c["oi"] for c in with_id if c["oi"] is not None}
    moved = sum(1 for i, v in kite_oi.items() if i in record_oi and v != record_oi[i])
    print(f"live OI differs from the record's for {moved} of {len(kite_oi)} contracts")

    spot = nifty["price"] if nifty and nifty.get("source") == "kite" and data["market_open"] else data["record_spot"]
    if spot is not None:
        for side, name in (("CE", "R"), ("PE", "S")):
            levels = top(contracts, kite_oi if data["market_open"] else {}, spot, side)
            print(f"{name}1-{name}3 at NIFTY {spot:,.1f}: " + ", ".join(f"{k:,.0f} {v / 1e5:.1f} L" for k, v in levels))


if __name__ == "__main__":
    asyncio.run(main())
