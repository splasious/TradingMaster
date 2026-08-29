import json

import httpx
import pytest

from app.services.broker.delta_broker import DeltaExchangeAPIError, DeltaExchangeBroker


def test_signature_matches_delta_documented_example():
    """Reproduces Delta's own documented signing example verbatim (verified
    live against their real API before writing the adapter -- see
    delta_broker.py's module docstring) so a future accidental change to
    the signing scheme fails loudly."""
    broker = DeltaExchangeBroker()
    broker._api_secret = "7b6f39dcf660ec1c7c664f612c60410a2bd0c258416b498bf0311f94228f"
    signature = broker._sign("GET", "/v2/orders", "?product_id=1&state=open", "", "1542110948")
    assert signature == "4e38dda3e6477092f360ba70399266d8145630b22bcc34c0ec7f804d5746877a"


def _mock_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, content=json.dumps(payload).encode(), request=httpx.Request("GET", "http://test"))


async def test_authenticate_success(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None):
        assert headers["api-key"] == "key123"
        assert "signature" in headers
        return _mock_response(200, {"success": True, "result": [{"asset_symbol": "USD", "balance": "1000", "available_balance": "900"}]})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = DeltaExchangeBroker()
    result = await broker.authenticate({"api_key": "key123", "api_secret": "secret456"})
    assert result is True


async def test_authenticate_missing_credentials_raises():
    broker = DeltaExchangeBroker()
    with pytest.raises(DeltaExchangeAPIError):
        await broker.authenticate({})


async def test_authenticate_surfaces_ip_whitelist_error_clearly(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None):
        return _mock_response(401, {"success": False, "error": {"code": "ip_not_whitelisted_for_api_key", "context": {"client_ip": "1.2.3.4"}}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = DeltaExchangeBroker()
    with pytest.raises(DeltaExchangeAPIError, match="not whitelisted"):
        await broker.authenticate({"api_key": "key123", "api_secret": "secret456"})


async def test_get_balance_parses_usd_asset(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None):
        return _mock_response(
            200,
            {"success": True, "result": [
                {"asset_symbol": "USD", "balance": "10000.50", "available_balance": "9500.25"},
                {"asset_symbol": "BTC", "balance": "0.5", "available_balance": "0.5"},
            ]},
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = DeltaExchangeBroker()
    broker._api_key, broker._api_secret = "k", "s"
    balance = await broker.get_balance()
    assert balance["available_margin"] == 9500.25
    assert balance["used_margin"] == pytest.approx(500.25)


async def test_place_order_returns_broker_order_id(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None):
        assert method == "POST"
        body = json.loads(content)
        assert body["side"] == "buy"
        assert body["product_id"] == 27
        return _mock_response(200, {"success": True, "result": {"id": 987654, "state": "open", "unfilled_size": 5}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = DeltaExchangeBroker()
    broker._api_key, broker._api_secret = "k", "s"
    result = await broker.place_order({"product_id": 27, "quantity": 5, "side": "buy", "order_type": "market_order"})
    assert result["broker_order_id"] == "987654"
    assert result["status"] == "open"


async def test_place_order_propagates_broker_rejection(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None):
        return _mock_response(400, {"success": False, "error": {"code": "insufficient_margin", "context": {}}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = DeltaExchangeBroker()
    broker._api_key, broker._api_secret = "k", "s"
    with pytest.raises(DeltaExchangeAPIError, match="insufficient_margin"):
        await broker.place_order({"product_id": 27, "quantity": 5, "side": "buy", "order_type": "market_order"})


async def test_cancel_order_requires_product_id_context():
    broker = DeltaExchangeBroker()
    broker._api_key, broker._api_secret = "k", "s"
    with pytest.raises(DeltaExchangeAPIError, match="product_id"):
        await broker.cancel_order("12345")


async def test_cancel_order_with_context_succeeds(monkeypatch):
    async def fake_request(self, method, url, headers=None, content=None):
        assert method == "DELETE"
        body = json.loads(content)
        assert body == {"id": 12345, "product_id": 27}
        return _mock_response(200, {"success": True, "result": {"state": "cancelled"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = DeltaExchangeBroker()
    broker._api_key, broker._api_secret = "k", "s"
    result = await broker.cancel_order("12345", context={"product_id": 27})
    assert result["status"] == "cancelled"


async def test_request_without_credentials_raises():
    broker = DeltaExchangeBroker()
    with pytest.raises(DeltaExchangeAPIError, match="Not authenticated"):
        await broker.get_positions()


async def test_get_historical_data_not_implemented_directs_to_data_source():
    broker = DeltaExchangeBroker()
    with pytest.raises(NotImplementedError):
        from datetime import datetime, timezone

        await broker.get_historical_data("BTCUSD", "1d", datetime.now(timezone.utc), datetime.now(timezone.utc))
