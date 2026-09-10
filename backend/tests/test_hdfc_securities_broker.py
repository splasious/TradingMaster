import httpx
import pytest

from app.services.broker.hdfc_securities_broker import HDFCSecuritiesAPIError, HDFCSecuritiesBroker, HDFCSecuritiesLoginRequired


def _mock_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload, request=httpx.Request("GET", "http://test"))


def test_build_login_url_includes_api_key():
    url = HDFCSecuritiesBroker.build_login_url("key123")
    assert url == "https://developer.hdfcsec.com/oapi/v1/login?api_key=key123"


async def test_authenticate_with_auth_code_exchanges_for_access_token(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, json=None):
        assert method == "POST"
        assert url == "https://developer.hdfcsec.com/oapi/v1/access-token"
        assert json["api_key"] == "key123"
        assert json["auth_code"] == "auth_abc"
        return _mock_response(200, {"data": {"access_token": "sess_tok_xyz"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = HDFCSecuritiesBroker()
    result = await broker.authenticate({"api_key": "key123", "api_secret": "secret456", "auth_code": "auth_abc"})
    assert result is True
    assert broker._access_token == "sess_tok_xyz"


async def test_authenticate_with_stored_access_token_verifies_via_profile(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, json=None):
        assert method == "GET"
        assert url == "https://developer.hdfcsec.com/oapi/v1/profile"
        assert headers["Authorization"] == "sess_tok_xyz"
        assert params["api_key"] == "key123"
        return _mock_response(200, {"data": {"client_id": "C123"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = HDFCSecuritiesBroker()
    result = await broker.authenticate({"api_key": "key123", "api_secret": "secret456", "access_token": "sess_tok_xyz"})
    assert result is True


async def test_authenticate_without_token_raises_login_required():
    broker = HDFCSecuritiesBroker()
    with pytest.raises(HDFCSecuritiesLoginRequired):
        await broker.authenticate({"api_key": "key123", "api_secret": "secret456"})


async def test_authenticate_missing_credentials_raises():
    broker = HDFCSecuritiesBroker()
    with pytest.raises(HDFCSecuritiesAPIError):
        await broker.authenticate({})


async def test_authenticate_surfaces_error_clearly(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, json=None):
        return _mock_response(400, {"status": "error", "message": "Invalid auth_code"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = HDFCSecuritiesBroker()
    with pytest.raises(HDFCSecuritiesAPIError, match="Invalid auth_code"):
        await broker.authenticate({"api_key": "key123", "api_secret": "secret456", "auth_code": "bad"})


async def test_place_order_translates_generic_side_and_order_type(monkeypatch):
    captured = {}

    async def fake_request(self, method, url, headers=None, params=None, json=None):
        captured["method"] = method
        captured["json"] = json
        return _mock_response(200, {"order_id": "hd123"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = HDFCSecuritiesBroker()
    broker._api_key = "key123"
    broker._access_token = "sess_tok"
    result = await broker.place_order({"tradingsymbol": "RELIANCE", "side": "buy", "quantity": 10, "order_type": "market"})

    assert captured["method"] == "POST"
    assert captured["json"]["transaction_type"] == "BUY"
    assert captured["json"]["order_type"] == "MARKET"
    assert captured["json"]["quantity"] == 10
    assert result == {"broker_order_id": "hd123", "status": "SUBMITTED", "raw": {"order_id": "hd123"}}


async def test_place_order_raises_when_no_order_id_returned(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, json=None):
        return _mock_response(200, {"unexpected": "shape"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = HDFCSecuritiesBroker()
    broker._api_key = "key123"
    with pytest.raises(HDFCSecuritiesAPIError, match="no recognizable order id"):
        await broker.place_order({"tradingsymbol": "RELIANCE", "side": "buy", "quantity": 10})


async def test_get_ltp_parses_quote(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, json=None):
        assert params["tradingsymbol"] == "RELIANCE"
        return _mock_response(200, {"last_price": 2500.5})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = HDFCSecuritiesBroker()
    broker._api_key = "key123"
    result = await broker.get_ltp("NSE", "RELIANCE")
    assert result["price"] == 2500.5


async def test_get_order_status_returns_status(monkeypatch):
    async def fake_request(self, method, url, headers=None, params=None, json=None):
        return _mock_response(200, {"status": "COMPLETE"})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    broker = HDFCSecuritiesBroker()
    broker._api_key = "key123"
    result = await broker.get_order_status("hd123")
    assert result == {"order_id": "hd123", "status": "COMPLETE", "raw": {"status": "COMPLETE"}}


async def test_request_without_credentials_raises():
    broker = HDFCSecuritiesBroker()
    with pytest.raises(HDFCSecuritiesAPIError, match="Not authenticated"):
        await broker.get_profile()
