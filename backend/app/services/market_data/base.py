"""Historical market-data source abstraction, mirroring the broker
abstraction (app.services.broker.base): the rest of the platform depends on
this interface, never on a specific provider. Delta Exchange is the only
adapter registered here today (see registry.py); Zerodha's NSE data goes
through its own broker-scoped path (services/broker/zerodha_broker.py) via
the Data Backfill Platform instead.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TypedDict


class Bar(TypedDict):
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None


class MarketDataSourceError(Exception):
    """Raised when a source is unreachable or returns an error the caller
    should surface, rather than a Python exception leaking implementation
    details (e.g. httpx internals)."""


class MarketDataSource(ABC):
    @abstractmethod
    async def get_historical_data(
        self, external_ref: str, timeframe: str, start: datetime | None, end: datetime | None
    ) -> list[Bar]: ...
