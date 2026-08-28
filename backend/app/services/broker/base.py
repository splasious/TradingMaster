"""Broker abstraction layer (PRD section 7).

Strategy logic must never be tightly coupled to a specific broker. Every
adapter — real (Zerodha Kite, Delta Exchange, added in later phases) or
simulated (MockBroker, used in this phase) — implements this exact
interface so the rest of the platform is broker-agnostic.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class BrokerInterface(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def authenticate(self, credentials: dict[str, Any]) -> bool: ...

    @abstractmethod
    async def get_profile(self) -> dict[str, Any]: ...

    @abstractmethod
    async def get_accounts(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_balance(self) -> dict[str, Any]: ...

    @abstractmethod
    async def get_positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_orders(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_trades(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_instruments(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_historical_data(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def subscribe_market_data(self, symbols: list[str]) -> None: ...

    @abstractmethod
    async def unsubscribe_market_data(self, symbols: list[str]) -> None: ...

    @abstractmethod
    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> dict[str, Any]: ...
