"""Real-time price engine for the Markets page WebSocket and paper trading
fills (PRD section 9).

Prices come from two places, and every message this engine emits is tagged
with which one actually produced it (PRD Rule 11: financial data
transparency -- never let the frontend mistake one for the other):

  - Real: `app.services.market_data.real_price_feed.RealPriceFeed` polls
    Yahoo Finance (NSE) and Delta Exchange's real public APIs on a periodic
    cycle and calls `set_real_price()` here. Once an instrument has a real
    price on file, this engine always serves that value (held flat between
    polls, never faked) tagged with its real source ("yahoo"/"delta") --
    it never reverts to the random walk just because a poll is briefly late.
  - Simulated: for instruments with no real source mapped (e.g. Zerodha, or
    anything RealPriceFeed hasn't covered yet), this engine falls back to a
    random walk seeded from the instrument's last known close, tagged
    "simulated" so it is never presented as real market data.
"""

import asyncio
import random
import uuid
from datetime import datetime, timezone

TICK_INTERVAL_SECONDS = 1.5


class TickEngine:
    def __init__(self) -> None:
        self._last_price: dict[uuid.UUID, float] = {}
        self._real_price: dict[uuid.UUID, float] = {}
        self._real_price_source: dict[uuid.UUID, str] = {}
        self._subscriber_counts: dict[uuid.UUID, int] = {}
        self._queues: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues.add(q)
        return q

    def unregister(self, q: asyncio.Queue, subscribed: set[uuid.UUID]) -> None:
        self._queues.discard(q)
        for instrument_id in subscribed:
            self._subscriber_counts[instrument_id] = max(0, self._subscriber_counts.get(instrument_id, 0) - 1)

    def subscribe(self, instrument_id: uuid.UUID, seed_price: float) -> None:
        self._subscriber_counts[instrument_id] = self._subscriber_counts.get(instrument_id, 0) + 1
        self._last_price.setdefault(instrument_id, seed_price)

    def unsubscribe(self, instrument_id: uuid.UUID) -> None:
        self._subscriber_counts[instrument_id] = max(0, self._subscriber_counts.get(instrument_id, 0) - 1)

    def get_current_price(self, instrument_id: uuid.UUID) -> float | None:
        """Non-WebSocket callers (e.g. the paper trading engine) read the
        latest known price directly rather than consuming a queue -- real
        if RealPriceFeed has one on file, simulated fallback otherwise."""
        return self._real_price.get(instrument_id, self._last_price.get(instrument_id))

    def set_real_price(self, instrument_id: uuid.UUID, price: float, source: str) -> None:
        """Called by RealPriceFeed with a genuine price polled from Yahoo
        or Delta. Once set, this instrument is served from here (flat
        between polls, never randomly perturbed) instead of the simulated
        random walk."""
        self._real_price[instrument_id] = price
        self._real_price_source[instrument_id] = source
        self._last_price[instrument_id] = price

    def _next_tick_message(self, instrument_id: uuid.UUID, now_iso: str) -> dict:
        if instrument_id in self._real_price:
            price = self._real_price[instrument_id]
            source = self._real_price_source[instrument_id]
        else:
            price = self._last_price[instrument_id]
            price = max(0.01, price * (1 + random.uniform(-0.0015, 0.0015)))
            self._last_price[instrument_id] = price
            source = "simulated"
        return {
            "type": "tick",
            "instrument_id": str(instrument_id),
            "price": round(price, 4),
            "ts": now_iso,
            "source": source,
        }

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            now = datetime.now(timezone.utc).isoformat()
            active = [iid for iid, count in self._subscriber_counts.items() if count > 0]
            for instrument_id in active:
                message = self._next_tick_message(instrument_id, now)
                for q in list(self._queues):
                    if not q.full():
                        q.put_nowait(message)

            heartbeat = {"type": "heartbeat", "ts": now}
            for q in list(self._queues):
                if not q.full():
                    q.put_nowait(heartbeat)


tick_engine = TickEngine()
