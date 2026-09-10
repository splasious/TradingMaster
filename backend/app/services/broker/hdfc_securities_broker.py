"""Real HDFC Securities (InvestRight Open API) broker adapter -- places
real orders with real money once connected through Settings > Brokers,
mirroring zerodha_broker.py's structure closely (both are OAuth-redirect
style, unlike Delta's per-request HMAC or Kotak Neo's TOTP+MPIN).

CONFIDENCE LEVEL -- read before relying on this for real capital:
HDFC Securities' developer docs (https://developer.hdfcsec.com/ir-docs/)
are a JavaScript-rendered site that could not be read directly while
building this (only static text extracted, no live endpoint reference).
What follows is confirmed via web search of HDFC's own developer-portal
description and third-party summaries, NOT by reading the raw API
reference the way Zerodha's adapter was (PRD Rule 1 note: this is the
weakest-verified adapter in this codebase -- treat it accordingly):

CONFIRMED (from HDFC's own developer-portal description):
  - App registration flow: log in to https://developer.hdfcsec.com/ with
    InvestRight credentials + OTP, accept the risk disclosure, then
    "Create App" with an App Name and a **Redirection URL** -- this is
    an OAuth-redirect pattern (api_key/api_secret issued per app, a
    hosted HDFC login page, then a redirect back to the app's own URL),
    the same shape as Zerodha's Kite Connect, not Delta's static
    key/secret pair.
  - Request auth pattern: requests carry `api_key` as a query parameter
    AND an `Authorization` header carrying an access_token (seen in a
    real example endpoint URL from HDFC's docs:
    `/oapi/v1/orders/:order_id/trades?api_key=<api_key>`, with the
    access_token in the Authorization header).
  - Base path is `/oapi/v1/...` off developer.hdfcsec.com.
  - Documented endpoint categories: Login, Fetch Profile, Place Order,
    Fetch Order Details, Fetch Tradebook, Positions, Holdings, Funds.

NOT CONFIRMED (inferred by REST convention, matching this codebase's
other adapters -- verify against a real registered app before trusting
real capital to it):
  - The exact login-redirect URL path and its query parameters.
  - The exact token-exchange endpoint path/body (assumed here to mirror
    Kite Connect's shape: POST an auth code plus api_key/api_secret,
    receive an access_token -- HDFC's own field names for the
    redirect-back token and the exchange call are NOT confirmed).
  - Exact JSON field names for place_order's request/response, and for
    profile/positions/holdings/funds responses (this adapter uses the
    same field vocabulary Zerodha's adapter uses -- tradingsymbol,
    exchange, transaction_type, quantity, product, order_type, validity
    -- as the most likely convention, not a confirmed one).
  A malformed field name against HDFC's real API should be rejected by
  their server with a clear 400-style error (broker APIs validate
  input), not silently misexecuted -- but this must still be verified
  with a real account and a tiny test order before any real deployment
  relies on it, exactly as this codebase's own Zerodha adapter docstring
  recommends for itself.

Credentials (api_key/api_secret, and once obtained, access_token) are
never stored by this class beyond the current process instance -- they
arrive via authenticate() from the Fernet-encrypted broker_credentials
table, the same mechanism every broker in this codebase uses.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.services.broker.base import BrokerInterface

_MAX_429_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 1.0

IST = timezone(timedelta(hours=5, minutes=30))


class HDFCSecuritiesAPIError(Exception):
    pass


class HDFCSecuritiesLoginRequired(HDFCSecuritiesAPIError):
    """Raised by authenticate() when only api_key/api_secret are available
    and no auth_code or previously-issued access_token was supplied --
    the interactive HDFC login step hasn't happened yet."""


class HDFCSecuritiesBroker(BrokerInterface):
    BASE_URL = "https://developer.hdfcsec.com/oapi/v1"
    LOGIN_URL = "https://developer.hdfcsec.com/oapi/v1/login"

    def __init__(self, broker_code: str = "hdfc_securities") -> None:
        self.broker_code = broker_code
        self._api_key: str | None = None
        self._api_secret: str | None = None
        self._access_token: str | None = None
        self._connected = False

    @classmethod
    def build_login_url(cls, api_key: str) -> str:
        # NOT CONFIRMED -- see module docstring. Mirrors the query-param
        # shape Kite Connect uses (api_key identifies which app's redirect
        # URL to send the user back to).
        return f"{cls.LOGIN_URL}?api_key={api_key}"

    @property
    def access_token(self) -> str | None:
        """Exposed so a /hdfc/callback endpoint can persist the session
        token obtained during authenticate() back into encrypted storage,
        the same reason ZerodhaKiteBroker.access_token exists."""
        return self._access_token

    async def _request(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> Any:
        if not self._api_key:
            raise HDFCSecuritiesAPIError("Not authenticated: call authenticate() with api_key/api_secret first")

        query = dict(params or {})
        query["api_key"] = self._api_key
        headers = {}
        if self._access_token:
            headers["Authorization"] = self._access_token

        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.request(method, f"{self.BASE_URL}{path}", headers=headers, params=query, json=json_body)
            except httpx.ConnectError as exc:
                raise HDFCSecuritiesAPIError("Could not reach HDFC Securities' API.") from exc
            except httpx.TimeoutException as exc:
                raise HDFCSecuritiesAPIError("HDFC Securities API request timed out.") from exc

            if resp.status_code == 429 and attempt < _MAX_429_RETRIES:
                attempt += 1
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else _RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                import asyncio

                await asyncio.sleep(delay)
                continue
            break

        try:
            body = resp.json()
        except ValueError as exc:
            raise HDFCSecuritiesAPIError(f"HDFC Securities returned a non-JSON response (HTTP {resp.status_code}).") from exc

        if resp.status_code >= 400 or (isinstance(body, dict) and body.get("status") == "error"):
            message = body.get("message") or body.get("error") or "unknown error" if isinstance(body, dict) else "unknown error"
            raise HDFCSecuritiesAPIError(f"HDFC Securities API error (HTTP {resp.status_code}): {message}")

        return body.get("data", body) if isinstance(body, dict) else body

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._access_token = None

    async def authenticate(self, credentials: dict[str, Any]) -> bool:
        api_key = credentials.get("api_key")
        api_secret = credentials.get("api_secret")
        if not api_key or not api_secret:
            raise HDFCSecuritiesAPIError("Both api_key and api_secret are required")
        self._api_key = api_key
        self._api_secret = api_secret

        access_token = credentials.get("access_token")
        auth_code = credentials.get("auth_code")

        # auth_code must win when both are present, for the same reason
        # request_token wins in zerodha_broker.py's authenticate(): it
        # only ever shows up here fresh from a just-completed interactive
        # login, while access_token may be yesterday's already-expired one
        # still sitting in the stored credential dict.
        if auth_code:
            data = await self._request(
                "POST", "/access-token",
                json_body={"api_key": api_key, "api_secret": api_secret, "auth_code": auth_code},
            )
            token = (data or {}).get("access_token") if isinstance(data, dict) else None
            if not token:
                raise HDFCSecuritiesAPIError(f"HDFC Securities token exchange did not return an access_token: {data}")
            self._access_token = token
            return True

        if access_token:
            self._access_token = access_token
            # A real authenticated call, not just "did we get a token" --
            # confirms the stored session is actually still valid.
            await self._request("GET", "/profile")
            return True

        raise HDFCSecuritiesLoginRequired(
            "No access_token or auth_code available -- complete the interactive HDFC Securities login first "
            "(use the login URL, then submit the resulting auth_code)."
        )

    async def get_profile(self) -> dict[str, Any]:
        result = await self._request("GET", "/profile")
        return result if isinstance(result, dict) else {"raw": result}

    async def get_accounts(self) -> list[dict[str, Any]]:
        profile = await self.get_profile()
        return [{"account_id": profile.get("client_id") or profile.get("user_id"), "type": "individual"}]

    async def get_balance(self) -> dict[str, Any]:
        result = await self._request("GET", "/funds")
        data = result if isinstance(result, dict) else {}
        available = data.get("available_margin") or data.get("available_balance") or 0.0
        used = data.get("used_margin") or data.get("utilised_margin") or 0.0
        return {"available_margin": float(available or 0.0), "used_margin": float(used or 0.0), "currency": "INR", "raw_margins": result}

    async def get_positions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/positions")
        return result if isinstance(result, list) else (result.get("positions", []) if isinstance(result, dict) else [])

    async def get_orders(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/orders")
        return result if isinstance(result, list) else (result.get("orders", []) if isinstance(result, dict) else [])

    async def get_trades(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/tradebook")
        return result if isinstance(result, list) else (result.get("trades", []) if isinstance(result, dict) else [])

    async def get_instruments(self) -> list[dict[str, Any]]:
        # Market-data/candle sourcing in this codebase always goes through
        # Zerodha regardless of which broker executes the order -- HDFC
        # Securities is an execution venue here, not a data source.
        return []

    async def get_historical_data(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        return []

    async def subscribe_market_data(self, symbols: list[str]) -> None:
        return None

    async def unsubscribe_market_data(self, symbols: list[str]) -> None:
        return None

    async def get_ltp(self, exchange: str, tradingsymbol: str) -> dict[str, Any]:
        """Real current price for one instrument, used by live trading to
        price a decision (see live_trading/oms.py's
        `_get_live_price_and_context`) -- same role as
        ZerodhaKiteBroker.get_ltp."""
        result = await self._request("GET", "/quote", params={"exchange": exchange, "tradingsymbol": tradingsymbol})
        data = result if isinstance(result, dict) else {}
        price = data.get("last_price") or data.get("ltp")
        if price is None:
            raise HDFCSecuritiesAPIError(f"No quote returned for {exchange}:{tradingsymbol}")
        return {"price": float(price)}

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        body = {
            "tradingsymbol": order["tradingsymbol"],
            "exchange": order.get("exchange", "NSE"),
            "transaction_type": order["side"].upper(),
            "order_type": {"market": "MARKET", "limit": "LIMIT"}.get(order.get("order_type", "market"), "MARKET"),
            "quantity": int(order["quantity"]),
            "product": order.get("product", "CNC"),
            "validity": order.get("validity", "DAY"),
        }
        if order.get("limit_price") is not None:
            body["price"] = str(order["limit_price"])
        if order.get("client_order_id"):
            body["tag"] = order["client_order_id"][:20]

        result = await self._request("POST", "/orders", json_body=body)
        data = result if isinstance(result, dict) else {}
        order_id = data.get("order_id") or data.get("orderId")
        if not order_id:
            raise HDFCSecuritiesAPIError(f"HDFC Securities place_order returned no recognizable order id: {result}")
        return {"broker_order_id": str(order_id), "status": "SUBMITTED", "raw": result}

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if "quantity" in changes:
            body["quantity"] = int(changes["quantity"])
        if "limit_price" in changes:
            body["price"] = str(changes["limit_price"])
        if "trigger_price" in changes:
            body["trigger_price"] = str(changes["trigger_price"])
        result = await self._request("PUT", f"/orders/{order_id}", json_body=body)
        return {"broker_order_id": str(order_id), "status": "MODIFY PENDING", "raw": result}

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self._request("DELETE", f"/orders/{order_id}")
        return {"broker_order_id": str(order_id), "status": "CANCEL PENDING", "raw": result}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/orders/{order_id}")
        data = result if isinstance(result, dict) else {}
        return {"order_id": order_id, "status": data.get("status", "unknown"), "raw": data}
