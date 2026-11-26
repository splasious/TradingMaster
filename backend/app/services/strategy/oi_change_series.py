"""OI Change Buy 15 MIN's change-in-OI chart (24 Nov), for the panel on its
card: each 15-minute trading candle's Put and Call dOI -- the PCR Analysis
tab's records closing 09:30 to 15:30 (NIFTY, 4 expiries, ATM +-40 strikes)
-- with the average the strategy decides on (versions 6-7: the last
AVERAGE_CANDLES candles; version 8, 26 Nov: 1, a candle is its own) and the
side each candle the strategy follows live (09:30-15:29: those closing
09:45-15:30) ended on.

The latest session with a record, and the BEFORE (4) candles before its
first, so the first closes' averages show where they come from. Read-only;
the rule itself is the strategy's own (native_strategies/
nifty_oi_change_buy.py: closes_a_candle, wanted_side, AVERAGE_CANDLES).
"""

from datetime import datetime

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.pcr import PcrSnapshot
from app.services.broker.zerodha_broker import IST
from app.services.strategy.native_strategies import nifty_oi_change_buy as oib

NAME = "OI Change Buy 15 MIN"
BEFORE = 4  # the previous session's last candles shown before the day's
FETCH = 80  # records read back: about three sessions


def is_charted(strategy_name: str | None) -> bool:
    return (strategy_name or "").strip().lower() == NAME.lower()


async def oi_change_series(db, now: datetime) -> dict:
    rows = (
        await db.execute(
            select(PcrSnapshot).where(
                PcrSnapshot.underlying == oib.PCR_UNDERLYING, PcrSnapshot.ts <= now,
                PcrSnapshot.put_oi_change.is_not(None), PcrSnapshot.call_oi_change.is_not(None),
            ).order_by(PcrSnapshot.ts.desc()).limit(FETCH)
        )
    ).scalars().all()
    candles = [snap for snap in reversed(rows) if oib.closes_a_candle(snap.ts)]
    out = {"average_candles": oib.AVERAGE_CANDLES, "session": None, "candles": []}
    if not candles:
        return out
    session = as_aware_utc(candles[-1].ts).astimezone(IST).date()
    first = next(i for i, snap in enumerate(candles) if as_aware_utc(snap.ts).astimezone(IST).date() == session)
    for i in range(max(0, first - BEFORE), len(candles)):
        snap = candles[i]
        window = candles[max(0, i - oib.AVERAGE_CANDLES + 1): i + 1]
        put_avg = sum(c.put_oi_change for c in window) / len(window)
        call_avg = sum(c.call_oi_change for c in window) / len(window)
        at = as_aware_utc(snap.ts).astimezone(IST)
        decides = at.date() == session and oib.FIRST_MARK < at.time() <= oib.CANDLES_TO  # version 8: followed live 09:30-15:29
        out["candles"].append({
            "at": as_aware_utc(snap.ts), "session": at.date(), "spot": snap.spot,
            "put_change": snap.put_oi_change, "call_change": snap.call_oi_change, "put_avg": put_avg, "call_avg": call_avg,
            "side": oib.wanted_side(put_avg, call_avg) if decides else None,
        })
    out["session"] = session
    return out
