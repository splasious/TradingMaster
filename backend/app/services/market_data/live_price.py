"""The price a strategy may trade at while NSE is open.

A strategy used to be handed TickEngine's current price, else the latest
stored candle close -- with no check on age. A contract or stock nothing had
asked for yet had no live price at that moment (the Kite WebSocket's
3,000-token budget doesn't reach every instrument, and the REST feed only
polls what's already been asked for), so the first read -- the entry --
got yesterday's close, or the simulated walk seeded from it. On 29 Sep that
put AM OP TRD 15 MIN's option legs and MACD - RSI - 15 MIN's ACUTAAS buy
at yesterday's prices.

So during market hours, for a Kite-priced instrument, only a live price
counts: a real tick or REST poll from the last LIVE_PRICE_MAX_AGE, else
Kite's last traded price fetched right now. If Kite can't answer there is
no price -- the strategy skips this tick and tries again on the next.
"""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import KiteAPIError
from app.services.fo_scan.pacing import quote_pacer
from app.services.market_data.hours import nse_market_open
from app.services.market_data.tick_engine import tick_engine
from app.services.options.pcr_snapshot_scheduler import kite_broker

# The REST feed re-polls every tracked instrument every 15 s, so a price
# older than this means nothing is updating it.
LIVE_PRICE_MAX_AGE = timedelta(seconds=60)


def needs_live_price(instrument: Instrument, now: datetime) -> bool:
    return instrument.data_source == "zerodha_kite" and nse_market_open(now)


async def live_price(db: AsyncSession, instrument: Instrument, now: datetime) -> float | None:
    price = tick_engine.get_fresh_real_price(instrument.id, LIVE_PRICE_MAX_AGE, now)
    if price is not None:
        return price
    broker = await kite_broker(db)
    if broker is None:
        return None
    key = f"{instrument.exchange}:{instrument.external_ref}"
    await quote_pacer.wait()
    try:
        price = (await broker.get_ltp_batch([key])).get(key)
    except KiteAPIError:
        return None
    if not price:
        return None
    tick_engine.set_real_price(instrument.id, float(price), source="kite_quote")
    return float(price)
