"""Deterministic simulated broker adapter.

Used in place of real Zerodha Kite / Delta Exchange adapters until those are
built (later phase, with real credentials). Never places a real order, never
calls a real API — it only proves the BrokerInterface contract end-to-end so
the rest of the platform (connection status, order flow shapes, later paper
trading) has something real to integrate against.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.broker.base import BrokerInterface


class MockBroker(BrokerInterface):
    def __init__(self, broker_code: str) -> None:
        self.broker_code = broker_code
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def authenticate(self, credentials: dict[str, Any]) -> bool:
        # A real adapter exchanges credentials for a session with the broker.
        # The mock accepts any non-empty payload so the connect flow can be
        # exercised without real API keys.
        return bool(credentials)

    async def get_profile(self) -> dict[str, Any]:
        return {"broker": self.broker_code, "name": "Mock Account", "user_id": "MOCK0001"}

    async def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": "MOCK0001", "type": "individual"}]

    async def get_balance(self) -> dict[str, Any]:
        return {"available_margin": 100000.0, "used_margin": 0.0, "currency": "INR"}

    async def get_positions(self) -> list[dict[str, Any]]:
        return []

    async def get_orders(self) -> list[dict[str, Any]]:
        return []

    async def get_trades(self) -> list[dict[str, Any]]:
        return []

    async def get_instruments(self) -> list[dict[str, Any]]:
        return []

    async def get_historical_data(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        return []

    async def subscribe_market_data(self, symbols: list[str]) -> None:
        return None

    async def unsubscribe_market_data(self, symbols: list[str]) -> None:
        return None

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "broker_order_id": f"MOCK-{uuid.uuid4().hex[:10].upper()}",
            "status": "REJECTED",
            "reason": "Live order placement is not available in this phase (mock broker).",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
        }

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return {"order_id": order_id, "status": "REJECTED", "reason": "Mock broker: no live orders exist."}

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"order_id": order_id, "status": "REJECTED", "reason": "Mock broker: no live orders exist."}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        return {"order_id": order_id, "status": "UNKNOWN"}
