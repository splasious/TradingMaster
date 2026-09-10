"""Real Kotak Neo Trade API broker adapter -- places real orders with real
money once connected through Settings > Brokers, mirroring zerodha_broker.py
and delta_broker.py.

Unlike Zerodha (raw HTTP, no official SDK) and Delta (raw HTTP, self-signed
HMAC), Kotak Neo publishes an official Python SDK
(https://github.com/Kotak-Neo/Kotak-neo-api-v2, org "Kotak-Neo" -- the
broker's own name, not a third-party clone) -- `neo_api_client`. This
adapter wraps that SDK rather than reimplementing its REST protocol by
hand (PRD Rule 1: never invent broker APIs -- delegating to the broker's
own maintained client is the strongest form of "not invented", stronger
than reading their docs and guessing at JSON field names).

Verified directly by reading the SDK's real source on GitHub (not
guessed):
  - `place_order`'s parameter validation (neo_api_client/req_data_validation.py):
    price/quantity/disclosed_quantity/trigger_price must be strings, not
    numbers; order_type must be one of "L"/"MKT"/"SL"/"SL-M"/"SP"/"2L"/"3L"
    (or their long-form aliases); transaction_type must be "B"/"S" (or
    "Buy"/"Sell"); validity must be "DAY" or "IOC".
  - `exchange_segment`/`product` accept the same short codes this codebase
    already uses elsewhere for Zerodha ("NSE", "CNC") -- confirmed via
    neo_api_client/settings.py's alias dicts (`exchange_segment["NSE"] ==
    "nse_cm"`, `product["CNC"] == "CNC"`).
  - Order book/status rows (from `order_report()`, backing
    `get_order_status` here) use `nOrdNo` (order number) and `ordSt`
    (status string) -- confirmed via neo_api_client/api/order_api.py's own
    order-verification logic, which indexes those exact keys.
  - `place_order` requires a completed 2FA session
    (`self.configuration.edit_token`/`edit_sid`, set by
    totp_login()+totp_validate()) or returns an error dict rather than
    raising -- this adapter checks for that explicitly rather than relying
    on an exception.

NOT verified against a live account (no Kotak Neo API subscription
available while building this): the exact top-level JSON shape of
`place_order`'s own success response (which field carries the new order's
`nOrdNo` -- assumed to match `order_report`'s convention, with a
defensive fallback scan if not), and `limits()`'s exact balance field
names (defensively extracted below; a live test is the remaining
verification step, same as Zerodha's own precedent in this codebase).
That balance-parsing uncertainty fails closed, not open: if the expected
field isn't found, `get_balance` returns 0 available margin, which makes
position sizing (see live_trading/oms.py's `_try_enter`) produce a zero
quantity and skip the entry -- never a wrong, guessed quantity.

Auth is TOTP + MPIN based, not Zerodha's OAuth-redirect:
  1. One-time, done by the user outside this app: register for TOTP on
     Kotak Neo's own site/app (scan a QR code into an authenticator app).
  2. Each authenticate() call here derives a fresh 6-digit TOTP code from
     the registered secret (via `pyotp`, standard library for this) --
     no interactive browser step needed, unlike Zerodha's daily login.
  3. `totp_login(mobile_number, ucc, totp)` -> view token + session id.
  4. `totp_validate(mpin)` -> trade token (`edit_token`/`edit_sid`), after
     which order placement/reporting calls are authorized.

Credentials (consumer_key, mobile_number, ucc, totp_secret, mpin) are
never stored by this class beyond the current process instance -- they
arrive via authenticate() from the Fernet-encrypted broker_credentials
table, the same mechanism every broker in this codebase uses. totp_secret
is the TOTP registration secret (a base32 string, the same one an
authenticator app would be given), not a live 6-digit code -- a live code
expires in ~30s and can't be stored for reuse.

The SDK is synchronous (plain `requests` underneath); every call into it
below runs via `asyncio.to_thread` so it never blocks this codebase's
async event loop, the same reason Zerodha/Delta's own httpx calls are
awaited natively.
"""

import asyncio
from datetime import datetime
from typing import Any

from app.services.broker.base import BrokerInterface

# Kotak Neo's order_type short codes (neo_api_client/settings.py's
# order_type_allowed_values) -- this codebase's own normalized order dict
# uses "market"/"limit" (see zerodha_broker.py's place_order), translated
# here the same way.
_ORDER_TYPE_MAP = {"market": "MKT", "limit": "L"}

# Kotak Neo's exchange_segment short codes accept "NSE"/"NFO" directly
# (settings.py's alias dict maps them to "nse_cm"/"nse_fo" internally) --
# no translation needed for the values this codebase already uses.


class KotakNeoAPIError(Exception):
    pass


class KotakNeoLoginRequired(KotakNeoAPIError):
    """Raised by authenticate() when required credential fields are
    missing -- distinct from a real authentication failure so calling
    code can show "missing setup" rather than "connection failed"."""


def _is_error_response(resp: Any) -> str | None:
    """Kotak Neo's SDK is inconsistent about signaling failure: some calls
    raise, most swallow exceptions and return a plain dict carrying
    'error'/'Error'/'Error Message' instead (confirmed in neo_api.py's own
    source, e.g. place_order's `except Exception as e: return {'Error': e}`
    and totp_login's `{'error': [{'message': ...}]}` for missing params).
    Returns a human-readable error string if `resp` looks like one of
    those, else None."""
    if not isinstance(resp, dict):
        return f"Unexpected response type from Kotak Neo: {type(resp).__name__}"
    if "error" in resp:
        err = resp["error"]
        if isinstance(err, list) and err:
            return str(err[0].get("message", err[0]))
        return str(err)
    if "Error" in resp:
        return str(resp["Error"])
    if "Error Message" in resp:
        return str(resp["Error Message"])
    return None


class KotakNeoBroker(BrokerInterface):
    def __init__(self, broker_code: str = "kotak_neo") -> None:
        self.broker_code = broker_code
        self._client: Any = None
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._client = None

    async def authenticate(self, credentials: dict[str, Any]) -> bool:
        consumer_key = credentials.get("consumer_key")
        mobile_number = credentials.get("mobile_number")
        ucc = credentials.get("ucc")
        totp_secret = credentials.get("totp_secret")
        mpin = credentials.get("mpin")
        if not all([consumer_key, mobile_number, ucc, totp_secret, mpin]):
            raise KotakNeoLoginRequired(
                "consumer_key, mobile_number, ucc, totp_secret, and mpin are all required -- "
                "generate the consumer_key from Kotak Neo's app/web (Invest > Trade API), and complete "
                "TOTP registration there first (the totp_secret is the same secret an authenticator app would use)."
            )

        def _login() -> Any:
            import pyotp
            from neo_api_client import NeoAPI

            client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None, consumer_key=consumer_key)
            totp_code = pyotp.TOTP(totp_secret).now()
            login_resp = client.totp_login(mobile_number=mobile_number, ucc=ucc, totp=totp_code)
            error = _is_error_response(login_resp)
            if error:
                raise KotakNeoAPIError(f"Kotak Neo TOTP login failed: {error}")
            validate_resp = client.totp_validate(mpin=mpin)
            error = _is_error_response(validate_resp)
            if error:
                raise KotakNeoAPIError(f"Kotak Neo MPIN validation failed: {error}")
            return client

        self._client = await asyncio.to_thread(_login)
        return True

    def _require_client(self) -> Any:
        if self._client is None:
            raise KotakNeoAPIError("Not authenticated: call authenticate() first")
        return self._client

    async def get_profile(self) -> dict[str, Any]:
        # Kotak Neo's SDK has no dedicated "profile" call distinct from the
        # totp_validate response -- limits() (segment/exchange/product
        # summary) is the closest always-available authenticated endpoint,
        # matching how this method is used elsewhere in this codebase
        # (a lightweight "is this session actually alive" probe).
        client = self._require_client()
        result = await asyncio.to_thread(lambda: client.limits(segment="ALL", exchange="ALL", product="ALL"))
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo profile/limits check failed: {error}")
        return result if isinstance(result, dict) else {"raw": result}

    async def get_accounts(self) -> list[dict[str, Any]]:
        return [{"account_id": "kotak_neo_account", "type": "individual"}]

    async def get_balance(self) -> dict[str, Any]:
        client = self._require_client()
        result = await asyncio.to_thread(lambda: client.limits(segment="ALL", exchange="ALL", product="ALL"))
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo get_balance failed: {error}")
        data = result.get("data", result) if isinstance(result, dict) else {}
        if isinstance(data, list):
            data = data[0] if data else {}
        # Field names here are not verified against a live account -- see
        # this module's docstring. Every candidate key tried is a
        # plausible Kotak/HS-platform convention; if none match, this
        # fails closed (0 available margin blocks new entries rather than
        # sizing one on a guess).
        available = data.get("Net") or data.get("net") or data.get("AvailableMargin") or data.get("available_margin") or 0.0
        used = data.get("MarginUsed") or data.get("used_margin") or 0.0
        return {
            "available_margin": float(available or 0.0), "used_margin": float(used or 0.0),
            "currency": "INR", "raw_margins": result,
        }

    async def get_positions(self) -> list[dict[str, Any]]:
        client = self._require_client()
        result = await asyncio.to_thread(client.positions)
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo get_positions failed: {error}")
        data = result.get("data", []) if isinstance(result, dict) else result
        return data if isinstance(data, list) else []

    async def get_orders(self) -> list[dict[str, Any]]:
        client = self._require_client()
        result = await asyncio.to_thread(client.order_report)
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo get_orders failed: {error}")
        data = result.get("data", []) if isinstance(result, dict) else result
        return data if isinstance(data, list) else []

    async def get_trades(self) -> list[dict[str, Any]]:
        client = self._require_client()
        result = await asyncio.to_thread(client.trade_report)
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo get_trades failed: {error}")
        data = result.get("data", []) if isinstance(result, dict) else result
        return data if isinstance(data, list) else []

    async def get_instruments(self) -> list[dict[str, Any]]:
        # Market-data/candle sourcing in this codebase always goes through
        # Zerodha (active_timeframe_sync_scheduler.py) regardless of which
        # broker executes the order -- Kotak Neo is an execution venue
        # here, not a data source, same division of responsibility Delta's
        # adapter already has for its own get_instruments.
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
        `_get_live_price_and_context`) -- Kotak Neo has no plain
        "quote by symbol" call; it needs a numeric scrip token first
        (search_scrip), then quotes() by that token, mirroring Zerodha's
        own token-lookup-then-quote pattern in this codebase."""
        client = self._require_client()
        segment = {"NSE": "nse_cm", "NFO": "nse_fo"}.get(exchange, "nse_cm")

        scrip_result = await asyncio.to_thread(
            lambda: client.search_scrip(exchange_segment=segment, symbol=tradingsymbol, expiry="", option_type="", strike_price="")
        )
        error = _is_error_response(scrip_result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo scrip search failed for '{tradingsymbol}': {error}")
        rows = scrip_result.get("data", scrip_result) if isinstance(scrip_result, dict) else scrip_result
        if not isinstance(rows, list) or not rows:
            raise KotakNeoAPIError(f"'{tradingsymbol}' not found in Kotak Neo's {segment} scrip master")
        match = rows[0]
        token = match.get("pSymbol") or match.get("instrument_token") or match.get("tk") or match.get("token")
        if not token:
            raise KotakNeoAPIError(f"Kotak Neo scrip search for '{tradingsymbol}' returned no instrument token: {match}")

        quote_result = await asyncio.to_thread(
            lambda: client.quotes(instrument_tokens=[{"instrument_token": str(token), "exchange_segment": segment}], quote_type="ltp")
        )
        error = _is_error_response(quote_result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo quote fetch failed for '{tradingsymbol}': {error}")
        quote_rows = quote_result.get("data", quote_result) if isinstance(quote_result, dict) else quote_result
        quote_row = quote_rows[0] if isinstance(quote_rows, list) and quote_rows else (quote_rows if isinstance(quote_rows, dict) else {})
        price = quote_row.get("last_traded_price") or quote_row.get("ltp")
        if price is None:
            raise KotakNeoAPIError(f"No LTP returned for {tradingsymbol}")
        return {"price": float(price), "instrument_token": token}

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        client = self._require_client()
        side = order["side"].upper()
        transaction_type = "B" if side == "BUY" else "S"
        order_type = _ORDER_TYPE_MAP.get(order.get("order_type", "market"), "MKT")
        price = str(order.get("limit_price") or "0")

        def _place() -> Any:
            return client.place_order(
                exchange_segment=order.get("exchange", "NSE"),
                product=order.get("product", "CNC"),
                price=price,
                order_type=order_type,
                quantity=str(int(order["quantity"])),
                validity=order.get("validity", "DAY"),
                trading_symbol=order["tradingsymbol"],
                transaction_type=transaction_type,
                amo="NO",
                tag=(order["client_order_id"][:20] if order.get("client_order_id") else None),
            )

        result = await asyncio.to_thread(_place)
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo place_order failed: {error}")

        data = result.get("data", result) if isinstance(result, dict) else result
        if isinstance(data, list):
            data = data[0] if data else {}
        order_id = (data or {}).get("nOrdNo") or (data or {}).get("norenordno") or (result or {}).get("nOrdNo")
        if not order_id:
            raise KotakNeoAPIError(f"Kotak Neo place_order returned no recognizable order id: {result}")
        return {"broker_order_id": str(order_id), "status": "SUBMITTED", "raw": result}

    async def modify_order(self, order_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        client = self._require_client()
        order_type = _ORDER_TYPE_MAP.get(changes.get("order_type"), "L") if changes.get("order_type") else "L"

        def _modify() -> Any:
            return client.modify_order(
                order_id=order_id,
                price=str(changes.get("limit_price", "0")),
                order_type=order_type,
                quantity=str(int(changes["quantity"])) if "quantity" in changes else "0",
                disclosed_quantity="0",
                trigger_price=str(changes.get("trigger_price", "0")),
                validity=changes.get("validity", "DAY"),
            )

        result = await asyncio.to_thread(_modify)
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo modify_order failed: {error}")
        return {"broker_order_id": str(order_id), "status": "MODIFY PENDING", "raw": result}

    async def cancel_order(self, order_id: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        client = self._require_client()
        result = await asyncio.to_thread(lambda: client.cancel_order(order_id=order_id))
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo cancel_order failed: {error}")
        return {"broker_order_id": str(order_id), "status": "CANCEL PENDING", "raw": result}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        client = self._require_client()
        result = await asyncio.to_thread(client.order_report)
        error = _is_error_response(result)
        if error:
            raise KotakNeoAPIError(f"Kotak Neo get_order_status failed: {error}")
        rows = result.get("data", []) if isinstance(result, dict) else result
        rows = rows if isinstance(rows, list) else []
        match = next((r for r in rows if str(r.get("nOrdNo")) == str(order_id)), None)
        status = match.get("ordSt", "unknown") if match else "unknown"
        return {"order_id": order_id, "status": status, "raw": match or {}}
