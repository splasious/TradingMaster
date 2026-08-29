import hashlib

import httpx
import pytest

from app.services.broker.zerodha_broker import KiteAPIError, KiteLoginRequired, ZerodhaKiteBroker


def test_checksum_matches_kite_documented_formula():
    """Kite Connect v3 documents the session-token checksum as
    SHA-256(api_key + request_token + api_secret) -- reproduced here as a
    literal, independently-computed oracle so a future accidental change
    to the formula fails loudly. Not verified against a live Kite account
    (no developer subscription available) -- this only proves the code
    matches the documented formula, not that Kite's server agrees."""
    expected = hashlib.sha256(b"key123req_tok_abcsecret456").hexdigest()
    assert ZerodhaKiteBroker._checksum("key123", "req_tok_abc", "secret456") == expected


def test_build_login_url_includes_api_key_and_version():
    url = ZerodhaKiteBroker.build_login_url("key123")
    assert url == "https://kite.zerodha.com/connect/login?v=3&api_key=key123"


def _mock_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("GET", "http://test"))


async def test_authenticate_with_request_token_exchanges_for_access_token(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        assert method == "POST"
        assert data["api_key"] == "key123"
        assert data["request_token"] == "req_tok_abc"
        assert data["checksum"] == hashlib.sha256(b"key123req_tok_abcsecret456").hexdigest()
        return _mock_response(200, {"status": "success", "data": {"access_token": "sess_tok_xyz", "user_id": "AB1234"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = ZerodhaKiteBroker()
    result = await broker.authenticate({"api_key": "key123", "api_secret": "secret456", "request_token": "req_tok_abc"})
    assert result is True
    assert broker._access_token == "sess_tok_xyz"


async def test_authenticate_with_stored_access_token_verifies_via_profile(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        assert method == "GET"
        assert url == "https://api.kite.trade/user/profile"
        assert headers["Authorization"] == "token key123:sess_tok_xyz"
        return _mock_response(200, {"status": "success", "data": {"user_id": "AB1234"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = ZerodhaKiteBroker()
    result = await broker.authenticate({"api_key": "key123", "api_secret": "secret456", "access_token": "sess_tok_xyz"})
    assert result is True


async def test_authenticate_without_token_raises_login_required():
    broker = ZerodhaKiteBroker()
    with pytest.raises(KiteLoginRequired):
        await broker.authenticate({"api_key": "key123", "api_secret": "secret456"})


async def test_authenticate_missing_credentials_raises():
    broker = ZerodhaKiteBroker()
    with pytest.raises(KiteAPIError):
        await broker.authenticate({})


async def test_authenticate_surfaces_kite_error_clearly(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        return _mock_response(403, {"status": "error", "error_type": "TokenException", "message": "Invalid request token"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = ZerodhaKiteBroker()
    with pytest.raises(KiteAPIError, match="Invalid request token"):
        await broker.authenticate({"api_key": "key123", "api_secret": "secret456", "request_token": "bad"})


async def test_get_balance_parses_equity_margins(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        return _mock_response(200, {
            "status": "success",
            "data": {"equity": {"available": {"live_balance": 50000.75}, "utilised": {"debits": 12000.25}}},
        })

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    balance = await broker.get_balance()
    assert balance["available_margin"] == 50000.75
    assert balance["used_margin"] == 12000.25
    assert balance["currency"] == "INR"


async def test_place_order_translates_generic_side_and_order_type(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        assert method == "POST"
        assert url == "https://api.kite.trade/orders/regular"
        assert data["transaction_type"] == "BUY"
        assert data["order_type"] == "MARKET"
        assert data["tradingsymbol"] == "INFY"
        assert data["exchange"] == "NSE"
        assert data["quantity"] == 10
        assert data["tag"] == "tm-abcdef01234567890"  # first 20 chars of the client_order_id below
        return _mock_response(200, {"status": "success", "data": {"order_id": "230925000012345"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    result = await broker.place_order({
        "tradingsymbol": "INFY", "exchange": "NSE", "side": "buy", "order_type": "market",
        "quantity": 10, "product": "CNC", "client_order_id": "tm-abcdef0123456789012",
    })
    assert result["broker_order_id"] == "230925000012345"


async def test_place_order_propagates_broker_rejection(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        return _mock_response(400, {"status": "error", "error_type": "InputException", "message": "Insufficient funds"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    with pytest.raises(KiteAPIError, match="Insufficient funds"):
        await broker.place_order({"tradingsymbol": "INFY", "exchange": "NSE", "side": "buy", "quantity": 10})


async def test_get_order_status_returns_latest_history_entry(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        return _mock_response(200, {"status": "success", "data": [
            {"status": "PUT ORDER REQ RECEIVED"},
            {"status": "OPEN"},
            {"status": "COMPLETE"},
        ]})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    result = await broker.get_order_status("230925000012345")
    assert result["status"] == "COMPLETE"


async def test_get_ltp_parses_quote(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        assert params == {"i": "NSE:INFY"}
        return _mock_response(200, {"status": "success", "data": {"NSE:INFY": {"last_price": 1502.35, "instrument_token": 408065}}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    quote = await broker.get_ltp("NSE", "INFY")
    assert quote["price"] == 1502.35
    assert quote["instrument_token"] == 408065


async def test_cancel_order_defaults_to_regular_variety(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, data=None):
        assert method == "DELETE"
        assert url == "https://api.kite.trade/orders/regular/12345"
        return _mock_response(200, {"status": "success", "data": {"order_id": "12345"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    result = await broker.cancel_order("12345")
    assert result["status"] == "CANCEL PENDING"


async def test_request_without_credentials_raises():
    broker = ZerodhaKiteBroker()
    with pytest.raises(KiteAPIError, match="Not authenticated"):
        await broker.get_positions()


async def test_get_historical_data_not_implemented_directs_to_data_source():
    broker = ZerodhaKiteBroker()
    with pytest.raises(NotImplementedError):
        from datetime import datetime, timezone

        await broker.get_historical_data("INFY", "1d", datetime.now(timezone.utc), datetime.now(timezone.utc))


async def test_get_instruments_parses_csv_response(monkeypatch):
    csv_body = "instrument_token,tradingsymbol,name,exchange\n408065,INFY,INFOSYS,NSE\n"

    async def fake_get(self, url, headers=None):
        assert url == "https://api.kite.trade/instruments/NSE"
        return httpx.Response(200, content=csv_body.encode(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = "k", "t"
    instruments = await broker.get_instruments()
    assert instruments == [{"instrument_token": "408065", "tradingsymbol": "INFY", "name": "INFOSYS", "exchange": "NSE"}]
