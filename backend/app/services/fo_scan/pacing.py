"""Kite's per-second limits, shared by everything the F&O scan does in the
same few seconds -- the OI store's captures and the strategy's own reads
all queue here, so a burst at 09:20 doesn't trip Kite's rate limit and
burn _request()'s 429 retries. /quote: one request a second, up to 500
instruments each. Historical candles: three requests a second."""

import asyncio
import time
from datetime import datetime

from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker

QUOTE_BATCH = 500


class Pacer:
    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


quote_pacer = Pacer(1.05)
history_pacer = Pacer(0.34)


async def kite_quotes(broker: ZerodhaKiteBroker, keys: list[str]) -> tuple[dict[str, dict], str | None]:
    """Quotes for "EXCHANGE:TRADINGSYMBOL" keys, 500 a request. A failed
    request's keys just come back missing; the last error is returned."""
    quotes: dict[str, dict] = {}
    error: str | None = None
    for i in range(0, len(keys), QUOTE_BATCH):
        await quote_pacer.wait()
        try:
            quotes.update(await broker.get_quote_batch(keys[i : i + QUOTE_BATCH]))
        except KiteAPIError as exc:
            error = str(exc)
    return quotes, error


async def kite_history(
    broker: ZerodhaKiteBroker, tradingsymbol: str, timeframe: str, start: datetime, end: datetime, exchange: str,
) -> list[dict]:
    await history_pacer.wait()
    return await broker.get_historical_data(tradingsymbol, timeframe, start, end, exchange)
