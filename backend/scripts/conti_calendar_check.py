"""Before a Conti Straddle duplicate with monthly buys (4 Nov): read-only facts.

- Conti Straddle 15 MIN: its versions, whether the newest one's code is the
  built-in file's (md5 only, "\r" stripped), its paper card(s) by status;
- NIFTY option expiries the app has contracts for, from today: how many
  contracts each, their strike range, lot size, and which is a month's last
  (the monthly);
- whether the running app streams those expiries: a few contracts near
  NIFTY of each expiry, over the market-data WebSocket for a few seconds.

Run inside the backend container (the health check's `conti_calendar_check`
input pipes it to `python -`). The public log gets counts, dates, md5s and
yes/no only -- no code, no user, no P&L.
"""

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

import websockets
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.paper_trading import PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.market_data.tick_engine import tick_engine  # noqa: F401 -- not this process's; ticks come over the WebSocket

NAMES = {"Conti Straddle 15 MIN": "03536e1f5a51afd155c25f286b3da832", "Conti Calendar 15 MIN": "9f14f718d0261ca858f5749f721192cf"}
LISTEN_SECONDS = 6


def md5(code: str | None) -> str:
    return hashlib.md5((code or "").replace("\r", "").encode()).hexdigest()


async def describe(db, NAME: str, FILE_MD5: str) -> None:
    strategies = (await db.execute(select(Strategy).where(func.trim(Strategy.name) == NAME))).scalars().all()
    print(f"strategies named {NAME!r}: {len(strategies)}")
    for s in strategies:
        versions = (await db.execute(
            select(StrategyVersion).where(StrategyVersion.strategy_id == s.id).order_by(StrategyVersion.version_number)
        )).scalars().all()
        cards = (await db.execute(
            select(PaperNativeDeployment.status, PaperNativeDeployment.strategy_version_id, func.count())
            .where(PaperNativeDeployment.strategy_id == s.id).group_by(PaperNativeDeployment.status, PaperNativeDeployment.strategy_version_id)
        )).all()
        by_id = {v.id: v.version_number for v in versions}
        print(f"  versions: {', '.join(f'v{v.version_number} {md5(v.python_code)[:8]} {v.timeframe}' for v in versions)}")
        newest = versions[-1] if versions else None
        print(f"  newest is the built-in file: {newest is not None and md5(newest.python_code) == FILE_MD5}")
        print(f"  paper cards: {', '.join(f'{st} on v{by_id.get(vid)} x{n}' for st, vid, n in cards) or 'none'}")
        runs = (await db.execute(select(PaperNativeDeployment).where(PaperNativeDeployment.strategy_id == s.id))).scalars().all()
        for card in runs:
            ran = card.last_evaluated_at.astimezone(IST).strftime("%d-%b %H:%M:%S") if card.last_evaluated_at else "never"
            print(f"  card {str(card.id)[:8]}: last run {ran}, last signal {card.last_signal or '-'}, "
                  f"position open: {bool((card.state or {}).get('position'))}")
            position = (card.state or {}).get("position")
            if NAME != "Conti Calendar 15 MIN":
                continue
            if position:
                opened = datetime.fromisoformat(position["opened_at"]).astimezone(IST).strftime("%d-%b %H:%M:%S")
                print(f"    opened {opened}: strike {position['strike']:,.0f} ({position['side']}-heavy, PCR {position['pcr_at_entry']:.3f}), "
                      f"weekly {position['weekly_expiry']}, monthly {position['monthly_expiry']}")
                for name, leg in position["legs"].items():
                    instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
                    print(f"    {name}: {leg['side']} {instrument.symbol if instrument else '?'} {leg['quantity'] / 65:g} lots at {leg['entry_price']:,.2f}")
                trades = (await db.execute(select(func.count()).select_from(PaperNativeTrade).where(PaperNativeTrade.deployment_id == card.id))).scalar()
                print(f"    closed trades so far: {trades}; take-profit last checked {position.get('checked_at', '-')}")
                seen = []
                for _ in range(4):  # how often it's re-checked: the check time, read once a second
                    await asyncio.sleep(1)
                    await db.refresh(card)
                    seen.append(((card.state or {}).get("position") or {}).get("checked_at", "-"))
                print(f"    re-read every second: {', '.join(seen)}")
            elif card.last_signal != "HOLD":
                reason = (card.last_signal_reason or "-").split(", P&L")[0]  # no P&L in this public log
                print(f"    last reason: {reason}")
            last = (await db.execute(
                select(PaperNativeTrade.exit_reason, PaperNativeTrade.closed_at, PaperNativeTrade.legs)
                .where(PaperNativeTrade.deployment_id == card.id).order_by(PaperNativeTrade.closed_at.desc()).limit(1)
            )).first()
            if last is not None:
                closed = last.closed_at.astimezone(IST).strftime("%d-%b %H:%M:%S") if last.closed_at.tzinfo else f"{last.closed_at:%d-%b %H:%M:%S} UTC"
                print(f"    last closed trade: {last.exit_reason} at {closed}, {len(last.legs)} legs, exits "
                      + ", ".join(f"{leg['side']} {leg['exit_price']:,.2f}" for leg in last.legs))
    pools = (await db.execute(select(func.count()).select_from(PaperPortfolio).where(func.trim(PaperPortfolio.name) == NAME))).scalar()
    print(f"  pools named {NAME!r}: {pools}")



async def main() -> None:
    today = datetime.now(timezone.utc).astimezone(IST).date()
    async with AsyncSessionLocal() as db:
        for NAME, FILE_MD5 in NAMES.items():
            await describe(db, NAME, FILE_MD5)

        nifty = (await db.execute(select(Instrument).where(Instrument.symbol == "NIFTY 50", Instrument.exchange == "NSE"))).scalars().first()
        rows = (await db.execute(
            select(Instrument.expiry, func.count(), func.min(Instrument.strike), func.max(Instrument.strike), func.max(Instrument.lot_size))
            .where(Instrument.underlying_instrument_id == nifty.id, Instrument.instrument_type == "option", Instrument.expiry >= today)
            .group_by(Instrument.expiry).order_by(Instrument.expiry)
        )).all()
        expiries = [r[0] for r in rows]
        monthly = {e for e in expiries if not any(o.year == e.year and o.month == e.month and o > e for o in expiries)}
        print(f"NIFTY option expiries from {today}: {len(rows)}")
        for e, n, lo, hi, lot in rows:
            print(f"  {e} {e:%a}: {n} contracts, strikes {lo:,.0f}-{hi:,.0f}, lot {lot}" + ("  <- last listed in its month" if e in monthly else ""))

        spot_row = (await db.execute(
            select(Instrument.strike).where(Instrument.underlying_instrument_id == nifty.id, Instrument.expiry == expiries[0])
        )).scalars().all() if expiries else []
        mid = sorted(spot_row)[len(spot_row) // 2] if spot_row else None
        sample: dict[str, str] = {}
        for e in expiries:
            for k in (mid - 50, mid, mid + 50) if mid else ():
                for t in ("CE", "PE"):
                    iid = (await db.execute(select(Instrument.id).where(
                        Instrument.underlying_instrument_id == nifty.id, Instrument.expiry == e, Instrument.strike == k, Instrument.option_type == t,
                    ))).scalars().first()
                    if iid:
                        sample[str(iid)] = str(e)
        user = (await db.execute(select(User).where(User.is_active.is_(True), User.is_approved.is_(True)).limit(1))).scalars().first()

    token = create_access_token(str(user.id), [])
    ticks: dict[str, dict] = {}
    async with websockets.connect(f"ws://localhost:8000/api/v1/ws/market-data?token={token}", max_size=2**22) as ws:
        await ws.send(json.dumps({"type": "subscribe", "instrument_ids": list(sample)}))
        deadline = time.monotonic() + LISTEN_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
            except asyncio.TimeoutError:
                break
            if message.get("type") == "tick":
                ticks[message["instrument_id"]] = message
    for e in expiries:
        ids = [i for i, x in sample.items() if x == str(e)]
        real = [i for i in ids if ticks.get(i, {}).get("source") == "kite"]
        print(f"  streaming {e}: {len(real)} of {len(ids)} sampled contracts with a Kite price")


if __name__ == "__main__":
    asyncio.run(main())
