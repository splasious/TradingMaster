"""OI Change Buy 15 MIN: the last sessions' 15-minute change in OI, for the
5-candle average review (24 Nov). Read-only, market data only.

Prints, for the last SESSIONS NIFTY sessions the server has change-in-OI
records for (pcr_snapshots: NIFTY, its 4 expiries, ATM +-40 strikes), every
record that closes a trading candle -- 09:30 to 15:30, each one's change
since the record before (the 15 minutes before) -- one line each:

  R,<session>,<HH:MM>,<NIFTY>,<put dOI 15m>,<call dOI 15m>

The 09:00 and 09:15 records (the overnight and pre-open changes) are left
out: they close no trading candle. The averages and the sides they pick are
worked out from these lines afterwards. Run inside the backend container
(the health check's `oi_avg_review` input pipes it to `python -`).
"""

import asyncio
from datetime import time

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.pcr import PcrSnapshot
from app.services.broker.zerodha_broker import IST

SESSIONS = 6  # five to show and the one before, whose last candles start the first one's average
FIRST, LAST = time(9, 30), time(15, 30)


async def main() -> None:
    async with AsyncSessionLocal() as db:
        days = (await db.execute(
            select(PcrSnapshot.session_date).where(PcrSnapshot.underlying == "NIFTY").distinct()
            .order_by(PcrSnapshot.session_date.desc()).limit(SESSIONS)
        )).scalars().all()
        rows = (await db.execute(
            select(PcrSnapshot).where(PcrSnapshot.underlying == "NIFTY", PcrSnapshot.session_date.in_(days))
            .order_by(PcrSnapshot.ts)
        )).scalars().all()
    shown = 0
    for snap in rows:
        at = snap.ts.astimezone(IST)
        if not FIRST <= at.time() <= LAST:
            continue
        put, call = snap.put_oi_change, snap.call_oi_change
        print(f"R,{snap.session_date},{at:%H:%M},{snap.spot or 0:.2f},"
              f"{'' if put is None else f'{put:.0f}'},{'' if call is None else f'{call:.0f}'}")
        shown += 1
    print(f"sessions {len(days)} ({min(days) if days else '-'} -> {max(days) if days else '-'}), candles {shown}")


if __name__ == "__main__":
    asyncio.run(main())
