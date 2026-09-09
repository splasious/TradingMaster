"""Option chain snapshot for the Options Dashboard's table -- one row per
strike, CE and PE legs side by side, seeded from the last backfilled
candle of each contract. The frontend layers live WebSocket ticks
(tick_engine, via kite_ticker_service) on top of this for real-time LTP/OI;
this snapshot only needs to supply a believable starting point plus a
"change" baseline.

Kite's own chain (and the reference screenshot) shows "change" against the
previous session's official close -- a convention we deliberately don't
try to replicate here, since getting that boundary subtly wrong would
misrepresent real money-relevant numbers. Instead "change" here is defined
plainly as *this leg's latest candle minus its own first candle of that
same calendar day* -- change since today's open, computed the same way
regardless of which timeframe a contract happened to be backfilled at.
"""

import uuid
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle


async def get_option_chain_snapshot(db: AsyncSession, underlying_instrument_id: uuid.UUID, expiry: date) -> list[dict]:
    option_rows = (
        await db.execute(
            select(Instrument.id, Instrument.symbol, Instrument.strike, Instrument.option_type).where(
                Instrument.underlying_instrument_id == underlying_instrument_id,
                Instrument.expiry == expiry,
                Instrument.instrument_type == "option",
            )
        )
    ).all()
    if not option_rows:
        return []
    leg_by_id = {i: (symbol, strike, option_type) for i, symbol, strike, option_type in option_rows}

    candle_rows = (
        await db.execute(
            select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.timeframe, OhlcvCandle.close, OhlcvCandle.open_interest)
            .where(OhlcvCandle.instrument_id.in_(leg_by_id.keys()))
            .order_by(OhlcvCandle.ts)
        )
    ).all()

    by_instrument: dict[uuid.UUID, list] = defaultdict(list)
    for inst_id, ts, timeframe, close, oi in candle_rows:
        by_instrument[inst_id].append((ts, timeframe, close, oi))

    legs: dict[float, dict] = {}
    for inst_id, rows in by_instrument.items():
        symbol, strike, option_type = leg_by_id[inst_id]
        latest_ts, latest_tf, latest_close, latest_oi = rows[-1]
        day_open_rows = [r for r in rows if r[1] == latest_tf and r[0].date() == latest_ts.date()]
        _, _, open_close, open_oi = day_open_rows[0] if day_open_rows else rows[-1]

        leg = {
            "instrument_id": str(inst_id),
            "symbol": symbol,
            "ltp": latest_close,
            "ltp_change": (latest_close - open_close) if latest_close is not None and open_close is not None else None,
            "open_interest": latest_oi,
            "open_interest_change": (latest_oi - open_oi) if latest_oi is not None and open_oi is not None else None,
            "as_of": latest_ts,
        }
        row = legs.setdefault(strike, {"strike": strike, "call": None, "put": None})
        row["call" if option_type == "CE" else "put"] = leg

    return [legs[strike] for strike in sorted(legs)]
