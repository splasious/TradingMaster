"""Real Delta Exchange India broker adapter -- places real orders with real
money once connected through Settings > Brokers. Authentication scheme
verified directly against Delta's live API before writing this (PRD Rule
1: never invent broker APIs): HMAC-SHA256 over
`method + timestamp + path + query_string + body`, sent as the
`api-key` / `signature` / `timestamp` headers, hex-encoded signature,
Unix-seconds timestamp, 5-second validity window. Endpoint paths and
request/response shapes below are Delta's documented ones, not guessed.

Credentials are never stored by this class beyond the current process
instance -- they arrive via `authenticate()` from the already-Fernet-
encrypted `broker_credentials` table (decrypted just before use), the same
mechanism built in Phase 1. This file never reads or writes a credential
to disk or logs one.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Any

import httpx

from app.services.broker.base import BrokerInterface


class DeltaExchangeAPIError(Exception):
    pass


class DeltaExchangeBroker(BrokerInterface):
    BASE_URL = "https://api.india.delta.exchange"

    def __init__(self, broker_code: str = "delta_exchange") -> None:
        self.broker_code = broker_code
        self._api_key: str | None = None
        self._api_secret: str | None = None
        self._connected = False

    def _sign(self, method: str, path: str, query: str, body: str, timestamp: str) -> str:
        message = method + timestamp + path + query + body
        return hmac.new(self._api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, query: str = "", json_body: dict | None = None) -> Any:
        if not self._api_key or not self._api_secret:
            raise DeltaExchangeAPIError("Not authenticated: call authenticate() with api_key/api_secret first")

        body_str = json.dumps(json_body) if json_body is not None else ""
        timestamp = str(int(time.time()))
        signature = self._sign(method, path, query, body_str, timestamp)
        headers = {
            "api-key": self._api_key,
            "signature": signature,
            "timestamp": timestamp,
            "User-Agent": "TradingMaster/1.0",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.request(
                    method, f"{self.BASE_URL}{path}{query}", headers=headers,
                    content=body_str if json_body is not None else None,
                )
        except httpx.ConnectError as exc:
            raise DeltaExchangeAPIError("Could not reach Delta Exchange's API.") from exc
        except httpx.TimeoutException as exc:
            raise DeltaExchangeAPIError("Delta Exchange API request timed out.") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise DeltaExchangeAPIError(f"Delta Exchange returned a non-JSON response (HTTP {resp.status_code}).") from exc

        if not data.get("success", False):
            error = data.get("error", {})
            code = error.get("code", "unknown_error")
            context = error.get("context", {})
            if code == "ip_not_whitelisted_for_api_key":
                client_ip = context.get("client_ip", "unknown")
                raise DeltaExchangeAPIError(
                    f"Delta Exchange rejected this request: this server's IP ({client_ip}) is not "
                    "whitelisted for the API key. Add it under Delta Exchange > Account > API Management, "
                    "or disable IP restriction for this key."
                )
            raise DeltaExchangeAPIError(f"Delta Exchange API error '{code}': {context}")

        return data.get("result")

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._api_key = None
        self._api_secret = None

    async def authenticate(self, credentials: dict[str, Any]) -> bool:
        api_key = credentials.get("api_key")
        api_secret = credentials.get("api_secret")
        if not api_key or not api_secret:
            raise DeltaExchangeAPIError("Both api_key and api_secret are required")
        self._api_key = api_key
        self._api_secret = api_secret
        # A real authenticated read-only call, not just "did we get a key" --
        # this is what actually proves the credentials work.
        await self._request("GET", "/v2/wallet/balances")
        return True

    async def get_profile(self) -> dict[str, Any]:
        balances = await self._request("GET", "/v2/wallet/balances")
        return {
            "broker": self.broker_code,
            "api_key_prefix": f"{self._api_key[:6]}..." if self._api_key else None,
            "asset_count": len(balances) if isinstance(balances, list) else 0,
        }

    async def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": self._api_key[:8] if self._api_key else None, "type": "individual"}]

    async def get_balance(self) -> dict[str, Any]:
        balances = await self._request("GET", "/v2/wallet/balances")
        usd = next((b for b in balances if b.get("asset_symbol") == "USD"), None)
        return {
            "available_margin": float(usd["available_balance"]) if usd else 0.0,
            "used_margin": float(usd["balance"]) - float(usd["available_balance"]) if usd else 0.0,
            "currency": "USD",
            "raw_balances": balances,
        }

    async def get_positions(self) -> list[dict[str, Any]]:
        positions = await self._request("GET", "/v2/positions")
        return positions or []

    async def get_orders(self) -> list[dict[str, Any]]:
        orders = await self._request("GET", "/v2/orders", "?state=open")
        return orders or []

    async def get_trades(self) -> list[dict[str, Any]]:
        fills = await self._request("GET", "/v2/fills")
        return fills or []

    async def get_instruments(self) -> list[dict[str, Any]]:
        products = await self._request("GET", "/v2/products", "?contract_types=perpetual_futures&states=live")
        return products or []

    async def get_historical_data(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        # Historical candles are served through DeltaExchangeDataSource
        # (services/market_data/delta_source.py, public endpoint, no auth
        # needed) -- this method exists only to satisfy BrokerInterface.
        raise NotImplementedError("Use services.market_data.delta_source.DeltaExchangeDataSource for historical data")

    async def subscribe_market_data(self, symbols: list[str]) -> None:
        # Live prices are served by the simulated tick engine
        # (services/market_data/tick_engine.py) until a real WebSocket feed
        # is wired up here in a later pass.
        return None

    async def unsubscribe_market_data(self, symbols: list[str]) -> None:
        return None

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        body = {
            "product_id": order["product_id"],
            "size": order["quantity"],
            "side": order["side"],
            "order_type": order.get("order_type", "market_order"),
        }
        if order.get("limit_price") is not None:
            body["limit_price"] = str(order["limit_price"])
        if order.get("client_order_id"):
            body["client_order_id"] = order["client_order_id"]
        if order.get("reduce_only"):
            body["reduce_only"] = True

        result = await self._request("POST", "/v2/orders", json_body=body)
        return {
            "broker_order_id": str(result["id"]),
            "status": result.get("state", "unknown"),
            "unfilled_size": result.get("unfilled_size"),
            "raw": result,
        }

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        body = {"id": int(order_id), "product_id": changes["product_id"]}
        if "limit_price" in changes:
            body["limit_price"] = str(changes["limit_price"])
        if "size" in changes:
            body["size"] = changes["size"]
        result = await self._request("PUT", "/v2/orders", json_body=body)
        return {"broker_order_id": str(result["id"]), "status": result.get("state", "unknown"), "raw": result}

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not context or "product_id" not in context:
            raise DeltaExchangeAPIError("Delta Exchange requires product_id to cancel an order (pass it via context)")
        result = await self._request("DELETE", "/v2/orders", json_body={"id": int(order_id), "product_id": context["product_id"]})
        return {"broker_order_id": str(order_id), "status": result.get("state", "cancelled") if result else "cancelled"}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/v2/orders/{order_id}")
        return {"order_id": order_id, "status": result.get("state", "unknown"), "raw": result}
