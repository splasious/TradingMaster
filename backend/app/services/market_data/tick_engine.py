"""Simulated real-time price engine (PRD section 9).

There is no live broker/data-provider tick feed wired up yet (that's a
later phase, once a real broker adapter replaces MockBroker). This engine
provides the same WebSocket protocol, subscribe/unsubscribe semantics, and
heartbeat/connection-status behavior that a real feed will use later, driven
by a random walk seeded from each instrument's last known close. Every
message this engine emits is tagged "source": "simulated" so the frontend
never presents it as real market data (PRD Rule 11: financial data
transparency).
"""

import asyncio
import random
import uuid
from datetime import datetime, timezone

TICK_INTERVAL_SECONDS = 1.5


class TickEngine:
    def __init__(self) -> None:
        self._last_price: dict[uuid.UUID, float] = {}
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

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            now = datetime.now(timezone.utc).isoformat()
            active = [iid for iid, count in self._subscriber_counts.items() if count > 0]
            for instrument_id in active:
                price = self._last_price[instrument_id]
                price = max(0.01, price * (1 + random.uniform(-0.0015, 0.0015)))
                self._last_price[instrument_id] = price
                message = {
                    "type": "tick",
                    "instrument_id": str(instrument_id),
                    "price": round(price, 4),
                    "ts": now,
                    "source": "simulated",
                }
                for q in list(self._queues):
                    if not q.full():
                        q.put_nowait(message)

            heartbeat = {"type": "heartbeat", "ts": now}
            for q in list(self._queues):
                if not q.full():
                    q.put_nowait(heartbeat)


tick_engine = TickEngine()
