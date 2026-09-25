"""Angel One and Dhan: connect-and-verify only (login, profile, funds) --
the requests match each broker's official SDK, and nothing that trades
can use them yet."""

import json

import httpx
import pytest
from httpx import AsyncClient

from app.services.broker import angel_one_broker
from app.services.broker.angel_one_broker import AngelOneAPIError, AngelOneBroker
from app.services.broker.dhan_broker import DhanAPIError, DhanBroker
from app.services.broker.registry import require_trading_support, supports_trading

TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # a valid base32 TOTP secret (pyotp's own example)


def _response(status_code: int, payload) -> httpx.Response:
    return httpx.Response(status_code, content=json.dumps(payload).encode(), request=httpx.Request("GET", "http://test"))


@pytest.fixture(autouse=True)
def _no_public_ip_lookup(monkeypatch):
    monkeypatch.setattr(angel_one_broker, "_public_ip", "203.0.113.7")


async def test_angel_one_logs_in_with_pin_and_a_fresh_totp_then_reads_funds(monkeypatch):
    calls = []

    async def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/loginByPassword"):
            return _response(200, {"status": True, "message": "SUCCESS", "data": {"jwtToken": "jwt-1", "refreshToken": "r", "feedToken": "f"}})
        return _response(200, {"status": True, "data": {"net": "150000.50", "availablecash": "125000.25", "utiliseddebits": "2000"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = AngelOneBroker()
    assert await broker.authenticate({"api_key": "key-1", "client_code": "A123", "pin": "1234", "totp_secret": TOTP_SECRET}) is True
    balance = await broker.get_balance()

    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword")
    assert kwargs["json"]["clientcode"] == "A123" and kwargs["json"]["password"] == "1234"
    assert len(kwargs["json"]["totp"]) == 6 and kwargs["json"]["totp"].isdigit()
    headers = kwargs["headers"]
    assert (headers["X-PrivateKey"], headers["X-UserType"], headers["X-SourceID"]) == ("key-1", "USER", "WEB")
    assert headers["X-ClientPublicIP"] == "203.0.113.7" and "Authorization" not in headers
    method, url, kwargs = calls[1]
    assert (method, url) == ("GET", "https://apiconnect.angelone.in/rest/secure/angelbroking/user/v1/getRMS")
    assert kwargs["headers"]["Authorization"] == "Bearer jwt-1"
    assert balance == {"available_margin": 125000.25, "used_margin": 2000.0, "currency": "INR"}


async def test_angel_one_login_rejection_is_shown_as_the_brokers_message(monkeypatch):
    async def fake_request(self, method, url, **kwargs):
        return _response(200, {"status": False, "message": "Invalid totp", "errorcode": "AB1050", "data": None})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(AngelOneAPIError, match="Invalid totp"):
        await AngelOneBroker().authenticate({"api_key": "k", "client_code": "A1", "pin": "1", "totp_secret": TOTP_SECRET})


async def test_angel_one_needs_every_field_and_a_real_totp_secret():
    with pytest.raises(AngelOneAPIError, match="all required"):
        await AngelOneBroker().authenticate({"api_key": "k", "client_code": "A1"})
    with pytest.raises(AngelOneAPIError, match="TOTP secret isn't valid"):
        await AngelOneBroker().authenticate({"api_key": "k", "client_code": "A1", "pin": "1", "totp_secret": "123456"})


async def test_angel_one_unknown_funds_fields_fail_closed(monkeypatch):
    async def fake_request(self, method, url, **kwargs):
        if url.endswith("/loginByPassword"):
            return _response(200, {"status": True, "data": {"jwtToken": "jwt"}})
        return _response(200, {"status": True, "data": {"somethingElse": "5"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = AngelOneBroker()
    await broker.authenticate({"api_key": "k", "client_code": "A1", "pin": "1", "totp_secret": TOTP_SECRET})
    assert (await broker.get_balance())["available_margin"] == 0.0


async def test_dhan_logs_in_with_pin_and_totp_then_reads_funds(monkeypatch):
    calls = []

    async def fake_request(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "generateAccessToken" in url:
            return _response(200, {"dhanClientId": "1100", "accessToken": "dhan-token"})
        return _response(200, {"dhanClientId": "1100", "availabelBalance": 98000.5, "utilizedAmount": 1500})

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    broker = DhanBroker()
    assert await broker.authenticate({"client_id": "1100", "pin": "4321", "totp_secret": TOTP_SECRET}) is True
    balance = await broker.get_balance()

    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", "https://auth.dhan.co/app/generateAccessToken")
    assert kwargs["params"]["dhanClientId"] == "1100" and kwargs["params"]["pin"] == "4321"
    assert len(kwargs["params"]["totp"]) == 6
    method, url, kwargs = calls[1]
    assert (method, url) == ("GET", "https://api.dhan.co/v2/fundlimit")
    assert kwargs["headers"]["access-token"] == "dhan-token" and kwargs["headers"]["client-id"] == "1100"
    assert balance == {"available_margin": 98000.5, "used_margin": 1500.0, "currency": "INR"}


async def test_dhan_login_errors_are_shown_and_a_missing_token_is_an_error(monkeypatch):
    async def rejected(self, method, url, **kwargs):
        return _response(401, {"errorType": "Invalid_Authentication", "errorCode": "DH-901", "errorMessage": "Invalid PIN"})

    monkeypatch.setattr(httpx.AsyncClient, "request", rejected)
    with pytest.raises(DhanAPIError, match="Invalid PIN"):
        await DhanBroker().authenticate({"client_id": "1100", "pin": "0", "totp_secret": TOTP_SECRET})

    async def no_token(self, method, url, **kwargs):
        return _response(200, {"status": "failure", "remarks": "TOTP expired"})

    monkeypatch.setattr(httpx.AsyncClient, "request", no_token)
    with pytest.raises(DhanAPIError, match="no access token"):
        await DhanBroker().authenticate({"client_id": "1100", "pin": "0", "totp_secret": TOTP_SECRET})


async def test_neither_broker_places_orders_yet():
    assert not supports_trading("angel_one") and not supports_trading("dhan")
    assert supports_trading("zerodha_kite") and supports_trading("kotak_neo")
    with pytest.raises(ValueError, match="Dhan is connected for login and funds only"):
        require_trading_support("dhan", "Dhan")
    with pytest.raises(NotImplementedError):
        await AngelOneBroker().place_order({})
    with pytest.raises(NotImplementedError):
        await DhanBroker().get_positions()


async def _headers(client: AsyncClient, admin: dict) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": admin["email"], "password": admin["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_connect_dhan_account_through_the_api(client: AsyncClient, seeded_admin: dict, monkeypatch):
    original = httpx.AsyncClient.request

    async def fake_request(self, method, url, **kwargs):
        if "dhan.co" in str(url):
            return _response(200, {"accessToken": "dhan-token"})
        return await original(self, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    headers = await _headers(client, seeded_admin)
    brokers = {b["code"]: b for b in (await client.get("/api/v1/brokers", headers=headers)).json()}
    assert (brokers["angel_one"]["name"], brokers["angel_one"]["supports_trading"]) == ("Angel One", False)
    assert (brokers["dhan"]["supports_trading"], brokers["zerodha_kite"]["supports_trading"]) == (False, True)

    resp = await client.post(
        "/api/v1/brokers/accounts",
        json={"broker_code": "dhan", "account_label": "My Dhan", "environment": "live",
              "credentials": {"client_id": "1100", "pin": "4321", "totp_secret": TOTP_SECRET}},
        headers=headers,
    )
    assert resp.status_code == 201
    account = resp.json()
    assert account["connection_status"] == "connected" and account["broker"]["supports_trading"] is False

    # Nothing that trades may use it: reconciliation says so plainly.
    resp = await client.get(f"/api/v1/live-trading/reconcile?broker_account_id={account['id']}", headers=headers)
    assert resp.status_code == 400 and "login and funds only" in resp.json()["detail"]
