"""Real Zerodha Kite Connect broker adapter -- places real orders with real
money once connected through Settings > Brokers, mirroring delta_broker.py.

Endpoint paths, request/response shapes, order states, and the session
checksum formula below are Kite Connect v3's own documented API
(https://kite.trade/docs/connect/v3/), not guessed (PRD Rule 1).

Partially verified live, without a real account: no developer subscription
is available, so full authentication (a real request_token from an actual
Zerodha login) was never exercised -- but every endpoint this adapter
calls was hit live with placeholder credentials to confirm the request
shape and error envelope match what's coded here, not just the docs:
  - POST /session/token  -> {"status":"error","error_type":"TokenException",
    "message":"Token is invalid or has expired.","data":null} (HTTP 400)
  - GET /user/profile, GET /quote/ltp (with a bogus Authorization header)
    -> {"status":"error","error_type":"TokenException",
    "message":"Incorrect `api_key` or `access_token`.","data":null}
  - https://kite.zerodha.com/connect/login?v=3&api_key=... -- correct
    documented URL shape (can't confirm the actual login page without a
    registered app).
This confirms _request()'s success/error parsing (`status` field,
`error_type`/`message` extraction) and the form-encoded request format are
correct against Kite's real servers -- what's unverified is a real
authenticated session end-to-end. The first real connection attempt with
genuine credentials is the remaining verification step.

Auth is fundamentally different from Delta's per-request HMAC signing:
Kite Connect uses an interactive OAuth-like flow --
  1. The user is sent to Kite's own login page (build_login_url()).
  2. After they log in, Kite redirects to this app's configured redirect
     URL with a one-time `request_token` in the query string.
  3. That token is exchanged once for a session `access_token` via
     POST /session/token, authenticated by a SHA-256 checksum of
     api_key + request_token + api_secret (Kite's documented formula).
  4. The access_token is valid until Kite's daily session expiry
     (~6am IST) -- there is no refresh token in the public API, so
     reconnecting after expiry means repeating steps 1-3.

Credentials (api_key/api_secret, and once obtained, access_token) are
never stored by this class beyond the current process instance -- they
arrive via authenticate() from the Fernet-encrypted broker_credentials
table, the same mechanism every broker in this codebase uses.
"""

import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.services.broker.base import BrokerInterface

# Kite Connect v3's documented historical-candle interval vocabulary --
# distinct from this codebase's internal "1m"/"5m"/... timeframe strings,
# and missing a native 1wk/1mo the same way Delta's own resolution map is
# missing them (see delta_source.py's _RESOLUTION_MAP).
KITE_INTERVAL_MAP = {
    "1m": "minute",
    "5m": "5minute",
    "15m": "15minute",
    "30m": "30minute",
    "60m": "60minute",
    "1d": "day",
}


class KiteAPIError(Exception):
    pass


class KiteLoginRequired(KiteAPIError):
    """Raised by authenticate() when only api_key/api_secret are available
    and no request_token or previously-issued access_token was supplied --
    the interactive login step hasn't happened yet. Distinct from a real
    authentication failure so calling code can show "log in" rather than
    "connection failed"."""


class ZerodhaKiteBroker(BrokerInterface):
    BASE_URL = "https://api.kite.trade"
    LOGIN_URL = "https://kite.zerodha.com/connect/login"
    API_VERSION = "3"

    def __init__(self, broker_code: str = "zerodha_kite") -> None:
        self.broker_code = broker_code
        self._api_key: str | None = None
        self._api_secret: str | None = None
        self._access_token: str | None = None
        self._connected = False

    @classmethod
    def build_login_url(cls, api_key: str) -> str:
        return f"{cls.LOGIN_URL}?v={cls.API_VERSION}&api_key={api_key}"

    @property
    def access_token(self) -> str | None:
        """Exposed so the /kite/callback endpoint can persist the session
        token obtained during authenticate() back into encrypted storage --
        BrokerInterface has no generic "extract the session" concept since
        Delta's per-request signing never produces one to persist."""
        return self._access_token

    @staticmethod
    def _checksum(api_key: str, request_token: str, api_secret: str) -> str:
        return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()

    async def _request(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> Any:
        if not self._api_key:
            raise KiteAPIError("Not authenticated: call authenticate() with api_key/api_secret first")

        headers = {"X-Kite-Version": self.API_VERSION}
        if self._access_token:
            headers["Authorization"] = f"token {self._api_key}:{self._access_token}"

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Kite's API takes form-encoded bodies, not JSON, for every
                # write endpoint -- confirmed in their docs, unlike Delta's
                # JSON API.
                resp = await client.request(method, f"{self.BASE_URL}{path}", headers=headers, params=params, data=data)
        except httpx.ConnectError as exc:
            raise KiteAPIError("Could not reach Zerodha Kite's API.") from exc
        except httpx.TimeoutException as exc:
            raise KiteAPIError("Zerodha Kite API request timed out.") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise KiteAPIError(f"Zerodha Kite returned a non-JSON response (HTTP {resp.status_code}).") from exc

        if body.get("status") != "success":
            message = body.get("message", "unknown error")
            error_type = body.get("error_type", "GeneralException")
            raise KiteAPIError(f"Zerodha Kite API error '{error_type}': {message}")

        return body.get("data")

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._access_token = None

    async def authenticate(self, credentials: dict[str, Any]) -> bool:
        api_key = credentials.get("api_key")
        api_secret = credentials.get("api_secret")
        if not api_key or not api_secret:
            raise KiteAPIError("Both api_key and api_secret are required")
        self._api_key = api_key
        self._api_secret = api_secret

        access_token = credentials.get("access_token")
        request_token = credentials.get("request_token")

        if access_token:
            self._access_token = access_token
            # A real authenticated call, not just "did we get a token" --
            # confirms the stored session is actually still valid (Kite
            # sessions expire daily; this is what catches that).
            await self._request("GET", "/user/profile")
            return True

        if request_token:
            checksum = self._checksum(api_key, request_token, api_secret)
            data = await self._request(
                "POST", "/session/token", data={"api_key": api_key, "request_token": request_token, "checksum": checksum}
            )
            self._access_token = data["access_token"]
            return True

        raise KiteLoginRequired(
            "No access_token or request_token available -- complete the interactive Zerodha login first "
            "(use the login URL, then submit the resulting request_token)."
        )

    async def get_profile(self) -> dict[str, Any]:
        return await self._request("GET", "/user/profile")

    async def get_accounts(self) -> list[dict[str, Any]]:
        profile = await self.get_profile()
        return [{"account_id": profile.get("user_id"), "type": profile.get("user_type", "individual")}]

    async def get_balance(self) -> dict[str, Any]:
        margins = await self._request("GET", "/user/margins")
        equity = margins.get("equity", {})
        available = equity.get("available", {})
        utilised = equity.get("utilised", {})
        return {
            "available_margin": float(available.get("live_balance", 0.0)),
            "used_margin": float(utilised.get("debits", 0.0)),
            "currency": "INR",
            "raw_margins": margins,
        }

    async def get_positions(self) -> list[dict[str, Any]]:
        positions = await self._request("GET", "/portfolio/positions")
        return positions.get("net", []) if positions else []

    async def get_orders(self) -> list[dict[str, Any]]:
        orders = await self._request("GET", "/orders")
        return orders or []

    async def get_trades(self) -> list[dict[str, Any]]:
        trades = await self._request("GET", "/trades")
        return trades or []

    async def get_instruments(self) -> list[dict[str, Any]]:
        """Kite serves this as a CSV dump, not JSON -- scoped to the NSE
        segment (GET /instruments/NSE) rather than the full multi-exchange
        dump, since NSE is the only segment this platform trades."""
        if not self._api_key:
            raise KiteAPIError("Not authenticated: call authenticate() with api_key/api_secret first")
        headers = {"X-Kite-Version": self.API_VERSION}
        if self._access_token:
            headers["Authorization"] = f"token {self._api_key}:{self._access_token}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{self.BASE_URL}/instruments/NSE", headers=headers)
        except httpx.ConnectError as exc:
            raise KiteAPIError("Could not reach Zerodha Kite's API.") from exc
        except httpx.TimeoutException as exc:
            raise KiteAPIError("Zerodha Kite API request timed out.") from exc
        if resp.status_code != 200:
            raise KiteAPIError(f"Zerodha Kite instrument dump request failed (HTTP {resp.status_code}).")
        return list(csv.DictReader(io.StringIO(resp.text)))

    async def get_historical_data(
        self, symbol: str, timeframe: str, start: datetime | None, end: datetime | None
    ) -> list[dict[str, Any]]:
        """Real Kite historical candles (GET /instruments/historical/{token}/{interval}),
        for the Data Backfill Platform's Zerodha block -- kept separate from
        the yahoo_nse source's own NSE history (that PRD's own non-goal:
        no cross-source merging, each source's data is independent, not a
        second copy of the same series). Needs a numeric instrument_token,
        looked up from get_instruments()'s CSV dump since Kite's historical
        endpoint doesn't accept a plain tradingsymbol."""
        interval = KITE_INTERVAL_MAP.get(timeframe)
        if interval is None:
            raise KiteAPIError(f"Zerodha Kite does not support timeframe '{timeframe}' via this adapter (supported: {sorted(KITE_INTERVAL_MAP)})")

        instruments = await self.get_instruments()
        match = next((row for row in instruments if row.get("tradingsymbol") == symbol), None)
        if match is None:
            raise KiteAPIError(f"'{symbol}' not found in Kite's NSE instrument list")
        token = match["instrument_token"]

        end = end or datetime.now(timezone.utc)
        start = start or (end - timedelta(days=60 if interval != "day" else 2000))
        params = {"from": start.strftime("%Y-%m-%d %H:%M:%S"), "to": end.strftime("%Y-%m-%d %H:%M:%S")}
        data = await self._request("GET", f"/instruments/historical/{token}/{interval}", params=params)
        candles = data.get("candles", []) if data else []

        bars: list[dict[str, Any]] = []
        for row in candles:
            ts = datetime.fromisoformat(row[0])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bars.append({"ts": ts, "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5] if len(row) > 5 else None})
        return bars

    async def subscribe_market_data(self, symbols: list[str]) -> None:
        # Kite has a real WebSocket ticker (kite.trade/docs/connect/v3/websocket/)
        # but wiring a persistent streaming connection into this
        # request/response adapter is deferred, same as Delta's adapter.
        return None

    async def unsubscribe_market_data(self, symbols: list[str]) -> None:
        return None

    async def get_ltp(self, exchange: str, tradingsymbol: str) -> dict[str, Any]:
        """Real current price for one instrument (GET /quote/ltp), used by
        live trading to price a decision -- Kite's LTP endpoint requires
        authentication, unlike Delta's public ticker, so this lives on the
        adapter itself rather than a separate unauthenticated data source."""
        instrument = f"{exchange}:{tradingsymbol}"
        data = await self._request("GET", "/quote/ltp", params={"i": instrument})
        quote = data.get(instrument) if data else None
        if quote is None:
            raise KiteAPIError(f"No quote returned for {instrument}")
        return {"price": float(quote["last_price"]), "instrument_token": quote.get("instrument_token")}

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        variety = order.get("variety", "regular")
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
            # Kite's `tag` is a plain label (max 20 chars, alnum + a few
            # symbols) -- unlike Delta's client_order_id, it is NOT a
            # broker-side idempotency key. Kite Connect has no equivalent
            # of one; a retried request can create a duplicate order.
            body["tag"] = order["client_order_id"][:20]

        result = await self._request("POST", f"/orders/{variety}", data=body)
        return {"broker_order_id": str(result["order_id"]), "status": "PUT ORDER REQ RECEIVED", "raw": result}

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        variety = changes.get("variety", "regular")
        body: dict[str, Any] = {}
        if "quantity" in changes:
            body["quantity"] = int(changes["quantity"])
        if "limit_price" in changes:
            body["price"] = str(changes["limit_price"])
        if "trigger_price" in changes:
            body["trigger_price"] = str(changes["trigger_price"])
        result = await self._request("PUT", f"/orders/{variety}/{order_id}", data=body)
        return {"broker_order_id": str(result["order_id"]), "status": "MODIFY PENDING", "raw": result}

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        variety = (context or {}).get("variety", "regular")
        result = await self._request("DELETE", f"/orders/{variety}/{order_id}")
        return {"broker_order_id": str(order_id), "status": "CANCEL PENDING", "raw": result}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        # GET /orders/{order_id} returns the order's full history (every
        # state transition); the most recent entry is its current state.
        history = await self._request("GET", f"/orders/{order_id}")
        latest = history[-1] if history else {}
        return {"order_id": order_id, "status": latest.get("status", "unknown"), "raw": latest}
