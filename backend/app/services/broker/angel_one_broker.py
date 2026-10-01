"""Angel One (formerly Angel Broking) SmartAPI adapter. It logs in, reads
the profile and funds, and -- for live native strategies only
(registry.py's supports_live_strategies) -- places, follows and cancels
orders and reads positions and holdings. The older trading paths (Live
Trading deployments, manual orders) still treat it as connect-only: they
speak Zerodha's contract names, and an Angel One order needs Angel's own
symbol and token (live_trading/broker_contracts.py supplies them to
native_gateway.py).

Written against Angel One's own official Python SDK
(github.com/angel-one/smartapi-python, SmartApi/smartConnect.py), calling
the same REST endpoints with httpx -- like zerodha_broker.py and
delta_broker.py -- rather than adding the SDK and its dependencies for
three calls. Read from that source, not guessed:
  - base URL https://apiconnect.angelone.in
  - login: POST /rest/auth/angelbroking/user/v1/loginByPassword with JSON
    {"clientcode", "password" (the login PIN), "totp"}; success carries
    data.jwtToken / refreshToken / feedToken
  - every request sends X-PrivateKey (the SmartAPI key), X-UserType "USER",
    X-SourceID "WEB", X-ClientLocalIP, X-ClientPublicIP, X-MACAddress,
    and once logged in "Authorization: Bearer <jwtToken>"
  - profile: GET /rest/secure/angelbroking/user/v1/getProfile
  - funds: GET /rest/secure/angelbroking/user/v1/getRMS
  - a failure carries "error_type" + "message", or "status": false

Orders, from the same SDK (SmartApi/smartConnect.py's routes and
placeOrder / cancelOrder / orderBook / position / holding, and its own
example order in test/api_test.py), not guessed:
  - place: POST /rest/secure/angelbroking/order/v1/placeOrder with
    variety NORMAL, tradingsymbol + symboltoken (Angel's own),
    transactiontype, exchange (NSE / NFO), ordertype, producttype
    (INTRADAY / CARRYFORWARD / DELIVERY), duration DAY, and price,
    quantity, squareoff, stoploss as strings; data.orderid comes back
  - cancel: POST .../order/v1/cancelOrder {variety, orderid}
  - order book: GET .../order/v1/getOrderBook -- an order's status is read
    from its row there (orderid, status, filledshares, averageprice, text)
  - positions: GET .../order/v1/getPosition; holdings: GET
    .../portfolio/v1/getHolding (data is null when there are none)
The order statuses are order_state_machine.ANGEL_ONE_STATE_MAP.

NOT verified against a live account (none available while building this):
getRMS's field names and the order-book / position / holding row fields
above (from SmartAPI's docs). They are read defensively; the balance fails
closed -- 0 available -- the same rule as kotak_neo_broker.py, and the
broker test (Settings > Brokers) runs a one-share order through all of
them before any strategy can use the account.

Auth is client code + login PIN + a TOTP code derived on each login from
the stored TOTP secret (pyotp), so there is no daily manual login.
"""

import re
import socket
import uuid
from datetime import datetime
from typing import Any

import httpx
import pyotp

from app.services.broker.base import BrokerInterface

_ROOT = "https://apiconnect.angelone.in"
_LOGIN = "/rest/auth/angelbroking/user/v1/loginByPassword"
_PROFILE = "/rest/secure/angelbroking/user/v1/getProfile"
_RMS = "/rest/secure/angelbroking/user/v1/getRMS"
_PLACE = "/rest/secure/angelbroking/order/v1/placeOrder"
_CANCEL = "/rest/secure/angelbroking/order/v1/cancelOrder"
_ORDER_BOOK = "/rest/secure/angelbroking/order/v1/getOrderBook"
_POSITIONS = "/rest/secure/angelbroking/order/v1/getPosition"
_HOLDINGS = "/rest/secure/angelbroking/portfolio/v1/getHolding"
# This app's (Kite's) product codes -> Angel One's.
_PRODUCTS = {"MIS": "INTRADAY", "NRML": "CARRYFORWARD", "CNC": "DELIVERY"}
_ORDER_TYPES = {"limit": "LIMIT", "market": "MARKET"}
_TIMEOUT = 15.0
_MAC = ":".join(re.findall("..", "%012x" % uuid.getnode()))
_NOT_ENABLED = "Angel One is connected for login, funds and live strategies only"


class AngelOneAPIError(Exception):
    pass


def _local_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


_public_ip: str | None = None


async def _public_ip_address() -> str:
    """The server's public IP, as the SDK sends it (it asks api.ipify.org)."""
    global _public_ip
    if _public_ip is None:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                _public_ip = (await client.get("https://api.ipify.org")).text.strip() or None
        except httpx.HTTPError:
            return "127.0.0.1"
    return _public_ip or "127.0.0.1"


def _amount(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class AngelOneBroker(BrokerInterface):
    def __init__(self, broker_code: str = "angel_one") -> None:
        self.broker_code = broker_code
        self._api_key: str | None = None
        self._client_code: str | None = None
        self._jwt: str | None = None
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._jwt = None

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": _local_ip(),
            "X-ClientPublicIP": await _public_ip_address(),
            "X-MACAddress": _MAC,
            "X-PrivateKey": self._api_key or "",
        }
        if self._jwt:
            headers["Authorization"] = f"Bearer {self._jwt}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(method, _ROOT + path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise AngelOneAPIError(f"Could not reach Angel One: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise AngelOneAPIError(f"Angel One returned an unreadable response (HTTP {resp.status_code})") from exc
        if not isinstance(data, dict):
            raise AngelOneAPIError(f"Angel One returned an unexpected response (HTTP {resp.status_code})")
        if data.get("error_type") or data.get("status") is False or resp.status_code >= 400:
            detail = data.get("message") or data.get("errorcode") or f"HTTP {resp.status_code}"
            raise AngelOneAPIError(f"Angel One: {detail}")
        return data

    async def authenticate(self, credentials: dict[str, Any]) -> bool:
        api_key = credentials.get("api_key")
        client_code = credentials.get("client_code")
        pin = credentials.get("pin")
        totp_secret = (credentials.get("totp_secret") or "").replace(" ", "")
        if not all([api_key, client_code, pin, totp_secret]):
            raise AngelOneAPIError(
                "SmartAPI key, client code, PIN and TOTP secret are all required -- the key is from "
                "smartapi.angelone.in (My Apps); the TOTP secret is shown when you enable TOTP on Angel One's site."
            )
        try:
            totp = pyotp.TOTP(totp_secret).now()
        except ValueError as exc:
            raise AngelOneAPIError("The TOTP secret isn't valid -- copy the key shown when enabling TOTP, not a 6-digit code") from exc
        self._api_key = api_key
        self._client_code = client_code
        self._jwt = None
        data = await self._call("POST", _LOGIN, {"clientcode": client_code, "password": pin, "totp": totp})
        token = (data.get("data") or {}).get("jwtToken")
        if not token:
            raise AngelOneAPIError("Angel One login returned no session")
        self._jwt = token
        return True

    def _require_session(self) -> None:
        if not self._jwt:
            raise AngelOneAPIError("Not authenticated: call authenticate() first")

    async def get_profile(self) -> dict[str, Any]:
        self._require_session()
        return (await self._call("GET", _PROFILE)).get("data") or {}

    async def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": self._client_code or "angel_one_account", "type": "individual"}]

    async def get_balance(self) -> dict[str, Any]:
        self._require_session()
        data = (await self._call("GET", _RMS)).get("data") or {}
        # Not verified against a live account (see the module docstring):
        # falls back to 0 available rather than a guessed number.
        available = _amount(data.get("availablecash")) or _amount(data.get("net"))
        return {"available_margin": available, "used_margin": _amount(data.get("utiliseddebits")), "currency": "INR"}

    async def _rows(self, path: str) -> list[dict[str, Any]]:
        self._require_session()
        data = (await self._call("GET", path)).get("data")
        return data if isinstance(data, list) else []

    async def get_positions(self) -> list[dict[str, Any]]:
        return await self._rows(_POSITIONS)

    async def get_holdings(self) -> list[dict[str, Any]]:
        return await self._rows(_HOLDINGS)

    async def get_orders(self) -> list[dict[str, Any]]:
        return await self._rows(_ORDER_BOOK)

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
        """The order dict native_gateway.py sends: `tradingsymbol` and
        `token` are Angel One's own (broker_contracts.py)."""
        self._require_session()
        if not order.get("token") or order.get("exchange") not in ("NSE", "NFO"):
            raise AngelOneAPIError("An Angel One order needs Angel's own symbol token and exchange")
        product = _PRODUCTS.get(order.get("product") or "")
        order_type = _ORDER_TYPES.get(order.get("order_type") or "")
        if product is None or order_type is None:
            raise AngelOneAPIError(f"Unsupported product/order type for Angel One: {order.get('product')}/{order.get('order_type')}")
        price = order.get("limit_price") or 0
        body = {
            "variety": "NORMAL",
            "tradingsymbol": order["tradingsymbol"],
            "symboltoken": str(order["token"]),
            "transactiontype": order["side"].upper(),
            "exchange": order["exchange"],
            "ordertype": order_type,
            "producttype": product,
            "duration": "DAY",
            "price": f"{float(price):.2f}",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(order["quantity"])),
        }
        if order.get("client_order_id"):
            body["ordertag"] = order["client_order_id"][:20]
        data = (await self._call("POST", _PLACE, body)).get("data") or {}
        order_id = data.get("orderid")
        if not order_id:
            raise AngelOneAPIError("Angel One returned no order id")
        return {"broker_order_id": str(order_id), "status": "put order req received", "raw": data}

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(_NOT_ENABLED)

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_session()
        data = await self._call("POST", _CANCEL, {"variety": "NORMAL", "orderid": order_id})
        return {"broker_order_id": str(order_id), "status": "cancel pending", "raw": data.get("data") or {}}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        row = next((r for r in await self.get_orders() if str(r.get("orderid")) == str(order_id)), None)
        status = str((row or {}).get("status") or (row or {}).get("orderstatus") or "unknown").lower()
        return {"order_id": order_id, "status": status, "raw": row or {}}
