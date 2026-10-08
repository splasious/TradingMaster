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

import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.services.broker.zerodha_broker import KiteAPIError, ltp_with_be_fallback
from app.services.fo_scan.pacing import quote_pacer
from app.services.market_data.hours import nse_market_open
from app.services.market_data.tick_engine import tick_engine
from app.services.options.pcr_snapshot_scheduler import kite_broker

logger = logging.getLogger(__name__)

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
    try:
        price = (await ltp_with_be_fallback(broker, [key], pace=quote_pacer.wait)).get(key)
    except KiteAPIError:
        return None
    if not price:
        return None
    tick_engine.set_real_price(instrument.id, float(price), source="kite_quote")
    return float(price)


# Kite's /quote/ltp takes at most this many instruments a request.
LTP_BATCH = 500
BATCH_ATTEMPTS = 2  # a failed batch is asked for once more before its instruments go without a price


async def live_prices(db: AsyncSession, instruments: list[Instrument], now: datetime) -> dict:
    """live_price() for many instruments at once -- one Kite request per
    LTP_BATCH instead of one per instrument, for a strategy that ranks
    hundreds of stocks at a candle close. {instrument_id: price}; one Kite
    has no price for (or every one, if Zerodha isn't logged in) is left out."""
    prices = {}
    missing = []
    for instrument in instruments:
        price = tick_engine.get_fresh_real_price(instrument.id, LIVE_PRICE_MAX_AGE, now)
        if price is not None:
            prices[instrument.id] = price
        else:
            missing.append(instrument)
    if not missing:
        return prices
    broker = await kite_broker(db)
    if broker is None:
        return prices
    for i in range(0, len(missing), LTP_BATCH):
        batch = {f"{inst.exchange}:{inst.external_ref}": inst for inst in missing[i : i + LTP_BATCH]}
        quotes = None
        for attempt in range(1, BATCH_ATTEMPTS + 1):
            try:
                quotes = await ltp_with_be_fallback(broker, list(batch), pace=quote_pacer.wait)
                break
            except KiteAPIError as exc:
                # Once left every instrument of the batch without a price, silently:
                # on 8 Oct an RS Rotation 15 MIN PCR exit kept 2 of 10 holdings.
                logger.warning("Kite LTP batch of %d failed (attempt %d of %d): %s", len(batch), attempt, BATCH_ATTEMPTS, type(exc).__name__)
        if quotes is None:
            continue
        for key, price in quotes.items():
            if price:
                tick_engine.set_real_price(batch[key].id, float(price), source="kite_quote")
                prices[batch[key].id] = float(price)
    return prices
