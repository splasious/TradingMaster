import sys
import types

import pytest

from app.services.broker.kotak_neo_broker import KotakNeoAPIError, KotakNeoBroker, KotakNeoLoginRequired, _is_error_response


def test_is_error_response_detects_all_known_shapes():
    assert _is_error_response({"error": [{"message": "bad totp"}]}) == "bad totp"
    assert _is_error_response({"Error": "boom"}) == "boom"
    assert _is_error_response({"Error Message": "Complete the 2fa process before accessing this application"}) is not None
    assert _is_error_response({"data": {"ok": True}}) is None
    assert _is_error_response("not a dict") is not None


class _FakeNeoAPI:
    """Stands in for neo_api_client.NeoAPI -- captures the exact
    totp_login/totp_validate calls so the test can assert on them without
    the real SDK (which isn't installed in this dev/test environment;
    only injected as a fake module here) or a live Kotak Neo account."""

    def __init__(self, environment=None, access_token=None, neo_fin_key=None, consumer_key=None):
        self.environment = environment
        self.consumer_key = consumer_key
        self.login_call = None
        self.validate_call = None

    def totp_login(self, mobile_number=None, ucc=None, totp=None):
        self.login_call = {"mobile_number": mobile_number, "ucc": ucc, "totp": totp}
        return {"data": {"token": "view_tok"}}

    def totp_validate(self, mpin=None):
        self.validate_call = {"mpin": mpin}
        return {"data": {"token": "trade_tok"}}


@pytest.fixture
def fake_sdk_modules(monkeypatch):
    """Injects fake `neo_api_client` and `pyotp` modules into sys.modules
    -- authenticate() does its SDK import lazily inside a closure
    (see kotak_neo_broker.py's module docstring for why), so this needs
    to happen before that import executes, not via a simple attribute
    monkeypatch."""
    fake_neo_module = types.SimpleNamespace(NeoAPI=_FakeNeoAPI)
    fake_pyotp_module = types.SimpleNamespace(TOTP=lambda secret: types.SimpleNamespace(now=lambda: "123456"))
    monkeypatch.setitem(sys.modules, "neo_api_client", fake_neo_module)
    monkeypatch.setitem(sys.modules, "pyotp", fake_pyotp_module)
    return fake_neo_module


async def test_authenticate_completes_totp_login_and_validate(fake_sdk_modules):
    broker = KotakNeoBroker()
    result = await broker.authenticate({
        "consumer_key": "ck123", "mobile_number": "+919999999999", "ucc": "ABC123",
        "totp_secret": "JBSWY3DPEHPK3PXP", "mpin": "123456",
    })
    assert result is True
    assert isinstance(broker._client, _FakeNeoAPI)
    assert broker._client.login_call == {"mobile_number": "+919999999999", "ucc": "ABC123", "totp": "123456"}
    assert broker._client.validate_call == {"mpin": "123456"}


async def test_authenticate_missing_field_raises_login_required():
    broker = KotakNeoBroker()
    with pytest.raises(KotakNeoLoginRequired):
        await broker.authenticate({"consumer_key": "ck123"})


async def test_authenticate_surfaces_totp_login_failure(monkeypatch, fake_sdk_modules):
    class _FailingLogin(_FakeNeoAPI):
        def totp_login(self, mobile_number=None, ucc=None, totp=None):
            return {"error": [{"message": "Invalid TOTP"}]}

    monkeypatch.setitem(sys.modules, "neo_api_client", types.SimpleNamespace(NeoAPI=_FailingLogin))
    broker = KotakNeoBroker()
    with pytest.raises(KotakNeoAPIError, match="Invalid TOTP"):
        await broker.authenticate({
            "consumer_key": "ck123", "mobile_number": "+919999999999", "ucc": "ABC123",
            "totp_secret": "JBSWY3DPEHPK3PXP", "mpin": "123456",
        })


class _FakeAuthenticatedClient:
    """Stands in for an already-logged-in NeoAPI instance -- used by
    place_order/get_order_status/etc. tests, which don't need to
    re-exercise the totp_login/totp_validate flow."""

    def __init__(self):
        self.place_order_call = None

    def place_order(self, **kwargs):
        self.place_order_call = kwargs
        return {"data": {"nOrdNo": "KN123"}}

    def order_report(self):
        return {"data": [{"nOrdNo": "KN123", "ordSt": "complete"}]}

    def cancel_order(self, order_id):
        return {"data": {"nOrdNo": order_id}}

    def limits(self, segment=None, exchange=None, product=None):
        return {"data": {"Net": "50000.0"}}


async def test_place_order_translates_generic_side_and_order_type():
    broker = KotakNeoBroker()
    client = _FakeAuthenticatedClient()
    broker._client = client

    result = await broker.place_order({
        "tradingsymbol": "RELIANCE-EQ", "exchange": "NSE", "side": "buy", "quantity": 10, "order_type": "market",
    })

    assert client.place_order_call["transaction_type"] == "B"
    assert client.place_order_call["order_type"] == "MKT"
    assert client.place_order_call["quantity"] == "10"
    assert client.place_order_call["price"] == "0"
    assert result == {"broker_order_id": "KN123", "status": "SUBMITTED", "raw": {"data": {"nOrdNo": "KN123"}}}


async def test_place_order_raises_without_authentication():
    broker = KotakNeoBroker()
    with pytest.raises(KotakNeoAPIError, match="Not authenticated"):
        await broker.place_order({"tradingsymbol": "X", "side": "buy", "quantity": 1})


async def test_get_order_status_finds_matching_row_by_norder_no():
    broker = KotakNeoBroker()
    broker._client = _FakeAuthenticatedClient()
    result = await broker.get_order_status("KN123")
    assert result["status"] == "complete"


async def test_get_balance_parses_limits_response():
    broker = KotakNeoBroker()
    broker._client = _FakeAuthenticatedClient()
    result = await broker.get_balance()
    assert result["available_margin"] == 50000.0
    assert result["currency"] == "INR"


async def test_cancel_order_returns_pending_status():
    broker = KotakNeoBroker()
    broker._client = _FakeAuthenticatedClient()
    result = await broker.cancel_order("KN123")
    assert result["broker_order_id"] == "KN123"
    assert result["status"] == "CANCEL PENDING"
