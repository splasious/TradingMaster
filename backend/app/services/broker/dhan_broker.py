"""Dhan (DhanHQ API v2) adapter -- Phase 1: connect and verify only. It
logs in, reads the profile and the funds; order placement, positions and
order status are not enabled yet (registry.py's _CONNECT_ONLY_BROKERS keeps
Live Trading, manual orders and reconciliation away from it until they are
built and checked against a real account).

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

NOT verified against a live account (none available while building this):
the token field in generateAccessToken's success response (the SDK itself
only notes it "usually returns accessToken") and fundlimit's field names.
Both are read defensively below; the balance fails closed -- 0 available --
the same rule as kotak_neo_broker.py.

Auth is client ID + login PIN + a TOTP code derived on each login from the
stored TOTP secret (pyotp), so there is no daily manual login and no
access token to paste.
"""

from datetime import datetime
from typing import Any

import httpx
import pyotp

from app.services.broker.base import BrokerInterface

_AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
_API = "https://api.dhan.co/v2"
_TIMEOUT = 15.0
_NOT_ENABLED = "Dhan is connected for login and funds only -- trading through it isn't enabled yet"


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

    async def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_ENABLED)

    async def get_orders(self) -> list[dict[str, Any]]:
        raise NotImplementedError(_NOT_ENABLED)

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
        raise NotImplementedError(_NOT_ENABLED)

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(_NOT_ENABLED)

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError(_NOT_ENABLED)

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError(_NOT_ENABLED)
