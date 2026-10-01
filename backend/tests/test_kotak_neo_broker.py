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

    def __init__(self, environment=None, access_token=None, neo_fin_key=None, consumer_key=None, **kwargs):
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

    def order_report(self, order_id=None):
        return {"data": [{"nOrdNo": "KN123", "ordSt": "complete"}]}

    def cancel_order(self, order_id):
        return {"data": {"nOrdNo": order_id}}

    def limits(self):
        return {"data": {"Net": "50000.0"}}


async def test_place_order_translates_generic_side_and_order_type():
    broker = KotakNeoBroker()
    client = _FakeAuthenticatedClient()
    broker._client = client

    result = await broker.place_order({
        "tradingsymbol": "RELIANCE-EQ", "exchange": "NSE", "side": "buy", "quantity": 10, "order_type": "market",
    })

    assert client.place_order_call["transaction_type"] == "B"
    assert client.place_order_call["exchange_segment"] == "nse_cm"  # SDK v3 refuses the "NSE" alias
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


# --- Against Kotak's real SDK (kotakneoapi 3.0.7), with a fake HTTP
# transport: the exact requests it would send to Kotak.

def _real_sdk(monkeypatch, replies):
    neo_api_client = pytest.importorskip("neo_api_client")
    import httpx

    sent = []

    def handler(request):
        sent.append(request)
        url = str(request.url)
        for fragment, payload in replies.items():
            if fragment in url:
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"stat": "Ok", "data": []})

    real = neo_api_client.NeoAPI

    class _Wired(real):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(neo_api_client, "NeoAPI", _Wired)
    return sent


def _form(request) -> dict:
    import json
    from urllib.parse import parse_qs

    return json.loads(parse_qs(request.content.decode())["jData"][0])


async def test_the_real_sdk_sends_a_protected_limit_in_kotaks_words(monkeypatch):
    sent = _real_sdk(monkeypatch, {
        "tradeApiLogin": {"data": {"token": "VIEW", "sid": "S1", "status": "success"}},
        "tradeApiValidate": {"data": {"token": "TRADE", "sid": "S2", "status": "success", "baseUrl": "https://cis.kotaksecurities.com"}},
        "/quick/order/rule/ms/place": {"stat": "Ok", "nOrdNo": "260101000000001", "stCode": 200},
        "/quick/user/orders/260101000000001": {"stat": "Ok", "data": [
            {"nOrdNo": "260101000000001", "ordSt": "complete", "fldQty": 65, "avgPrc": "150.85"}]},
        "/quick/user/positions": {"stat": "Ok", "data": [
            {"exSeg": "nse_fo", "tok": "40001", "trdSym": "NIFTY2610622700CE", "flBuyQty": "65", "flSellQty": "0",
             "cfBuyQty": "0", "cfSellQty": "0"}]},
        "masterscrip/file-paths": {"data": {"filesPaths": ["https://x/2026-10-01/transformed/nse_cm-v1.csv",
                                                          "https://x/2026-10-01/transformed/nse_fo.csv"]}},
    })
    broker = KotakNeoBroker()
    await broker.authenticate({"consumer_key": "CK", "mobile_number": "+919999999999", "ucc": "ABC12",
                               "totp_secret": "JBSWY3DPEHPK3PXP", "mpin": "1234"})
    placed = await broker.place_order({
        "tradingsymbol": "NIFTY2610622700CE", "exchange": "nse_fo", "token": "40001", "product": "NRML", "quantity": 65.0,
        "side": "buy", "order_type": "limit", "limit_price": 151.5, "client_order_id": "tmn-0123456789abcdef0123",
    })
    assert placed["broker_order_id"] == "260101000000001"
    order = next(r for r in sent if r.url.path.endswith("/quick/order/rule/ms/place"))
    assert (order.headers["Authorization"], order.headers["Auth"], order.headers["Sid"]) == ("CK", "TRADE", "S2")
    assert _form(order) == {"am": "NO", "dq": "0", "es": "nse_fo", "mp": "0", "pc": "NRML", "pr": "151.50", "pt": "L", "qt": "65",
                            "rt": "DAY", "tp": "0", "ts": "NIFTY2610622700CE", "tt": "B", "ig": "tmn-0123456789abcdef",
                            "os": "NEOTRADEAPI"}

    status = await broker.get_order_status("260101000000001")
    assert (status["status"], status["raw"]["fldQty"], status["raw"]["avgPrc"]) == ("complete", 65, "150.85")
    assert any(r.url.path.endswith("/quick/user/orders/260101000000001") for r in sent)  # the one order, not the book

    await broker.cancel_order("260101000000001")
    cancel = next(r for r in sent if r.url.path.endswith("/quick/order/cancel"))
    assert _form(cancel) == {"on": "260101000000001", "am": "NO"}

    assert (await broker.get_positions())[0]["tok"] == "40001"
    assert await broker.contract_list_urls() == {"nse_cm": "https://x/2026-10-01/transformed/nse_cm-v1.csv",
                                                 "nse_fo": "https://x/2026-10-01/transformed/nse_fo.csv"}
    await broker.get_balance()
    limits = next(r for r in sent if r.url.path.endswith("/quick/user/limits"))
    assert _form(limits) == {"seg": "ALL", "exch": "ALL", "prod": "ALL"}
