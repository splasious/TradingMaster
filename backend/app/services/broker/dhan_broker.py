"""Dhan (DhanHQ API v2) adapter. It logs in, reads the profile and funds,
and -- for live native strategies only (registry.py's
supports_live_strategies) -- places, follows and cancels orders and reads
positions and holdings. The older trading paths (Live Trading
deployments, manual orders) still treat it as connect-only: they speak
Zerodha's contract names, and a Dhan order needs Dhan's own security id
(live_trading/broker_contracts.py supplies it to native_gateway.py).

Written against Dhan's own official Python SDK (the `dhanhq` package on
PyPI, 2.2.0: dhanhq/auth.py and dhanhq/dhan_http.py), calling the same
REST endpoints with httpx -- like zerodha_broker.py and delta_broker.py.
Read from that source, not guessed:
  - login by PIN + TOTP: POST https://auth.dhan.co/app/generateAccessToken
    with query parameters dhanClientId, pin, totp (DhanLogin.generate_token)
  - profile: GET https://api.dhan.co/v2/profile with headers access-token
    and dhanClientId (DhanLogin.user_profile)
  - funds: GET https://api.dhan.co/v2/fundlimit with headers access-token,
    client-id, Content-type and Accept (DhanHTTP + Funds.get_fund_limits)
  - a failure is a non-2xx status with errorCode / errorType / errorMessage

Orders, read from the same SDK (dhanhq/_order.py, _portfolio.py,
dhan_http.py), not guessed:
  - place: POST /v2/orders, JSON with dhanClientId, transactionType
    (BUY/SELL), exchangeSegment (NSE_EQ / NSE_FNO), productType (CNC /
    INTRADAY / MARGIN), orderType, validity, securityId, quantity,
    disclosedQuantity, price, afterMarketOrder, triggerPrice and an
    optional correlationId -- the SDK's own payload, field for field
  - status: GET /v2/orders/{order-id}; cancel: DELETE /v2/orders/{order-id}
  - positions: GET /v2/positions; holdings: GET /v2/holdings
  - headers on all of them: access-token, client-id, Content-type, Accept
From Dhan's v2 docs (as published, not readable from this sandbox):
orderId / orderStatus in the place response; orderStatus, filledQty,
averageTradedPrice, omsErrorDescription in an order report; netQty,
securityId, exchangeSegment in a position; securityId, totalQty in a
holding. The order statuses are order_state_machine.DHAN_STATE_MAP.

NOT verified against a live account (none available while building this):
the token field in generateAccessToken's success response (the SDK itself
only notes it "usually returns accessToken"), fundlimit's field names, and
the order / position / holding fields above. They're read defensively; the
balance fails closed -- 0 available -- the same rule as kotak_neo_broker.py,
and the broker test (Settings > Brokers) runs a one-share order through
all of them before any strategy can use the account.

Auth is client ID + login PIN + a TOTP code derived on each login from the
stored TOTP secret (pyotp), so there is no daily manual login and no
access token to paste.
"""

import json
from datetime import datetime
from typing import Any

import httpx
import pyotp

from app.services.broker.base import BrokerInterface

_AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
_API = "https://api.dhan.co/v2"
_TIMEOUT = 15.0
_NOT_ENABLED = "Dhan is connected for login, funds and live strategies only"
# This app's (Kite's) product codes -> Dhan's.
_PRODUCTS = {"MIS": "INTRADAY", "NRML": "MARGIN", "CNC": "CNC"}
_ORDER_TYPES = {"limit": "LIMIT", "market": "MARKET"}


class DhanAPIError(Exception):
    pass


def _amount(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _error_detail(data: Any, status_code: int) -> str:
    if isinstance(data, dict):
        for key in ("errorMessage", "message", "remarks", "errorCode"):
            if data.get(key):
                return str(data[key])
    return f"HTTP {status_code}"


async def _request(method: str, url: str, **kwargs: Any) -> Any:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise DhanAPIError(f"Could not reach Dhan: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise DhanAPIError(f"Dhan returned an unreadable response (HTTP {resp.status_code})") from exc
    if not 200 <= resp.status_code < 300:
        raise DhanAPIError(f"Dhan: {_error_detail(data, resp.status_code)}")
    return data


class DhanBroker(BrokerInterface):
    def __init__(self, broker_code: str = "dhan") -> None:
        self.broker_code = broker_code
        self._client_id: str | None = None
        self._access_token: str | None = None
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._access_token = None

    async def authenticate(self, credentials: dict[str, Any]) -> bool:
        client_id = credentials.get("client_id")
        pin = credentials.get("pin")
        totp_secret = (credentials.get("totp_secret") or "").replace(" ", "")
        if not all([client_id, pin, totp_secret]):
            raise DhanAPIError(
                "Client ID, PIN and TOTP secret are all required -- the TOTP secret is shown when you set up "
                "TOTP in Dhan's app or website."
            )
        try:
            totp = pyotp.TOTP(totp_secret).now()
        except ValueError as exc:
            raise DhanAPIError("The TOTP secret isn't valid -- copy the key shown when setting up TOTP, not a 6-digit code") from exc
        self._client_id = client_id
        self._access_token = None
        data = await _request("POST", _AUTH_URL, params={"dhanClientId": client_id, "pin": pin, "totp": totp})
        token = None
        if isinstance(data, dict):
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            token = data.get("accessToken") or data.get("access_token") or nested.get("accessToken")
        if not token:
            raise DhanAPIError(f"Dhan login returned no access token: {_error_detail(data, 200)}")
        self._access_token = token
        return True

    def _require_session(self) -> None:
        if not self._access_token:
            raise DhanAPIError("Not authenticated: call authenticate() first")

    async def get_profile(self) -> dict[str, Any]:
        self._require_session()
        data = await _request("GET", f"{_API}/profile", headers={"access-token": self._access_token, "dhanClientId": self._client_id})
        return data if isinstance(data, dict) else {}

    async def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": self._client_id or "dhan_account", "type": "individual"}]

    async def get_balance(self) -> dict[str, Any]:
        self._require_session()
        data = await _request(
            "GET", f"{_API}/fundlimit",
            headers={"access-token": self._access_token, "client-id": self._client_id, "Content-type": "application/json", "Accept": "application/json"},
        )
        data = data if isinstance(data, dict) else {}
        # Not verified against a live account (see the module docstring).
        # "availabelBalance" is Dhan's own spelling; falls back to 0
        # available rather than a guessed number.
        available = _amount(data.get("availabelBalance")) or _amount(data.get("availableBalance"))
        return {"available_margin": available, "used_margin": _amount(data.get("utilizedAmount")), "currency": "INR"}

    def _headers(self) -> dict[str, str]:
        self._require_session()
        return {"access-token": self._access_token, "client-id": self._client_id, "Content-type": "application/json",
                "Accept": "application/json"}

    async def get_positions(self) -> list[dict[str, Any]]:
        data = await _request("GET", f"{_API}/positions", headers=self._headers())
        return data if isinstance(data, list) else []

    async def get_holdings(self) -> list[dict[str, Any]]:
        try:
            data = await _request("GET", f"{_API}/holdings", headers=self._headers())
        except DhanAPIError as exc:
            # Dhan answers an empty demat account with an error rather than [].
            if "no holding" in str(exc).lower():
                return []
            raise
        return data if isinstance(data, list) else []

    async def get_orders(self) -> list[dict[str, Any]]:
        data = await _request("GET", f"{_API}/orders", headers=self._headers())
        return data if isinstance(data, list) else []

    async def get_trades(self) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_ENABLED)

    async def get_instruments(self) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_ENABLED)

    async def get_historical_data(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_ENABLED)

    async def subscribe_market_data(self, symbols: list[str]) -> None:
        raise NotImplementedError(_NOT_ENABLED)

    async def unsubscribe_market_data(self, symbols: list[str]) -> None:
        raise NotImplementedError(_NOT_ENABLED)

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """The order dict native_gateway.py sends: `token` is Dhan's
        security id and `exchange` its segment (broker_contracts.py)."""
        if not order.get("token") or order.get("exchange") not in ("NSE_EQ", "NSE_FNO"):
            raise DhanAPIError("A Dhan order needs Dhan's own security id and segment")
        product = _PRODUCTS.get(order.get("product") or "")
        order_type = _ORDER_TYPES.get(order.get("order_type") or "")
        if product is None or order_type is None:
            raise DhanAPIError(f"Unsupported product/order type for Dhan: {order.get('product')}/{order.get('order_type')}")
        payload = {
            "dhanClientId": self._client_id,
            "transactionType": order["side"].upper(),
            "exchangeSegment": order["exchange"],
            "productType": product,
            "orderType": order_type,
            "validity": "DAY",
            "securityId": str(order["token"]),
            "quantity": int(order["quantity"]),
            "disclosedQuantity": 0,
            "price": float(order.get("limit_price") or 0),
            "afterMarketOrder": False,
            "boProfitValue": None,
            "boStopLossValue": None,
            "triggerPrice": 0.0,
        }
        if order.get("client_order_id"):
            payload["correlationId"] = order["client_order_id"][:20]
        data = await _request("POST", f"{_API}/orders", headers=self._headers(), content=json.dumps(payload))
        order_id = data.get("orderId") if isinstance(data, dict) else None
        if not order_id:
            raise DhanAPIError(f"Dhan returned no order id: {_error_detail(data, 200)}")
        return {"broker_order_id": str(order_id), "status": (data.get("orderStatus") or "TRANSIT"), "raw": data}

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(_NOT_ENABLED)

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        data = await _request("DELETE", f"{_API}/orders/{order_id}", headers=self._headers())
        return {"broker_order_id": str(order_id), "status": "CANCEL PENDING", "raw": data}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        data = await _request("GET", f"{_API}/orders/{order_id}", headers=self._headers())
        if isinstance(data, list):  # one order, sometimes wrapped in a list
            data = data[0] if data else {}
        data = data if isinstance(data, dict) else {}
        return {"order_id": order_id, "status": data.get("orderStatus", "unknown"), "raw": data}
