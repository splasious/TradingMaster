"""Live probe of Zerodha Kite's own historical-data depth for one NFO
option contract -- distinct from OhlcvCandle's backfilled range (what
this app has already stored), this asks Kite's API directly what IT has,
through the already-connected Zerodha session (Settings > Brokers), the
same way a user could check by logging into Kite themselves.

An option contract only ever trades from its own listing date to its own
expiry -- typically a few weeks for a weekly contract, a few months for a
monthly one -- so this is naturally a much shorter window than an equity
or index's history. This reports the real number Kite returns rather than
guessing it from that general rule.
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker


async def get_history_depth(db: AsyncSession, underlying_instrument_id: uuid.UUID, expiry: date) -> dict:
    # Probing every leg in an expiry would mean dozens of live Kite API
    # calls for one dashboard question -- one representative contract
    # (the lowest-strike CE, a stable and reproducible pick) is enough to
    # answer "how far back does data go for this expiry", since every
    # contract in the same expiry was listed and expires on essentially
    # the same schedule.
    leg = (
        await db.execute(
            select(Instrument.id, Instrument.external_ref, Instrument.symbol).where(
                Instrument.underlying_instrument_id == underlying_instrument_id,
                Instrument.expiry == expiry,
                Instrument.instrument_type == "option",
                Instrument.option_type == "CE",
            ).order_by(Instrument.strike).limit(1)
        )
    ).first()
    if leg is None:
        return {"symbol": None, "error": "No option contracts backfilled for this expiry", "our_earliest": None, "our_latest": None, "our_candle_count": 0}
    instrument_id, external_ref, symbol = leg

    our_ts = (
        await db.execute(select(OhlcvCandle.ts).where(OhlcvCandle.instrument_id == instrument_id).order_by(OhlcvCandle.ts))
    ).scalars().all()
    our_earliest = our_ts[0] if our_ts else None
    our_latest = our_ts[-1] if our_ts else None

    result = {
        "symbol": symbol, "our_earliest": our_earliest, "our_latest": our_latest, "our_candle_count": len(our_ts),
        "kite_earliest": None, "kite_latest": None, "kite_candle_count": None, "error": None,
    }

    creds = await find_connected_zerodha_credentials(db)
    if creds is None:
        result["error"] = "No connected Zerodha account -- log in under Settings > Brokers to query Kite directly"
        return result

    broker = ZerodhaKiteBroker()
    broker._api_key = creds["api_key"]
    broker._access_token = creds["access_token"]
    try:
        # No start/end -- get_historical_data's own default (2000 days
        # back, for the "1d" interval) safely covers any realistic option
        # contract's lifetime; Kite just returns whatever actually exists
        # inside that window, which is exactly the number being asked for.
        bars = await broker.get_historical_data(external_ref, "1d", None, None, "NFO")
    except KiteAPIError as exc:
        result["error"] = str(exc)
        return result

    result["kite_earliest"] = bars[0]["ts"] if bars else None
    result["kite_latest"] = bars[-1]["ts"] if bars else None
    result["kite_candle_count"] = len(bars)
    return result
