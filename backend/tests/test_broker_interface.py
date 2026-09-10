import inspect

import pytest

from app.services.broker.base import BrokerInterface
from app.services.broker.delta_broker import DeltaExchangeBroker
from app.services.broker.hdfc_securities_broker import HDFCSecuritiesBroker
from app.services.broker.kotak_neo_broker import KotakNeoBroker
from app.services.broker.mock_broker import MockBroker
from app.services.broker.registry import get_broker_adapter, is_real_adapter, requires_interactive_auth
from app.services.broker.zerodha_broker import ZerodhaKiteBroker


def test_mock_broker_implements_full_interface():
    interface_methods = {name for name, _ in inspect.getmembers(BrokerInterface, predicate=inspect.isfunction)}
    mock = MockBroker("zerodha_kite")
    for name in interface_methods:
        assert hasattr(mock, name)
        assert inspect.iscoroutinefunction(getattr(mock, name))


def test_delta_broker_implements_full_interface():
    interface_methods = {name for name, _ in inspect.getmembers(BrokerInterface, predicate=inspect.isfunction)}
    delta = DeltaExchangeBroker("delta_exchange")
    for name in interface_methods:
        assert hasattr(delta, name)
        assert inspect.iscoroutinefunction(getattr(delta, name))


def test_zerodha_broker_implements_full_interface():
    interface_methods = {name for name, _ in inspect.getmembers(BrokerInterface, predicate=inspect.isfunction)}
    kite = ZerodhaKiteBroker("zerodha_kite")
    for name in interface_methods:
        assert hasattr(kite, name)
        assert inspect.iscoroutinefunction(getattr(kite, name))


def test_hdfc_broker_implements_full_interface():
    interface_methods = {name for name, _ in inspect.getmembers(BrokerInterface, predicate=inspect.isfunction)}
    hdfc = HDFCSecuritiesBroker("hdfc_securities")
    for name in interface_methods:
        assert hasattr(hdfc, name)
        assert inspect.iscoroutinefunction(getattr(hdfc, name))


def test_kotak_neo_broker_implements_full_interface():
    interface_methods = {name for name, _ in inspect.getmembers(BrokerInterface, predicate=inspect.isfunction)}
    kotak = KotakNeoBroker("kotak_neo")
    for name in interface_methods:
        assert hasattr(kotak, name)
        assert inspect.iscoroutinefunction(getattr(kotak, name))


def test_registry_returns_broker_interface_instances():
    for code in ("zerodha_kite", "delta_exchange", "hdfc_securities", "kotak_neo"):
        adapter = get_broker_adapter(code)
        assert isinstance(adapter, BrokerInterface)
    assert is_real_adapter("zerodha_kite") is True  # real Kite Connect adapter, written to spec
    assert is_real_adapter("delta_exchange") is True  # Phase 7: real order placement, live-verified
    assert is_real_adapter("hdfc_securities") is True
    assert is_real_adapter("kotak_neo") is True
    assert requires_interactive_auth("zerodha_kite") is True  # needs a browser login, not just a key/secret
    assert requires_interactive_auth("delta_exchange") is False
    assert requires_interactive_auth("hdfc_securities") is True  # same OAuth-redirect shape as Kite
    assert requires_interactive_auth("kotak_neo") is False  # TOTP+MPIN completes in one authenticate() call


def test_registry_rejects_unknown_broker():
    with pytest.raises(ValueError):
        get_broker_adapter("some_unregistered_broker")


async def test_mock_broker_never_places_a_real_order():
    mock = MockBroker("zerodha_kite")
    await mock.connect()
    result = await mock.place_order({"symbol": "NIFTY", "side": "BUY", "quantity": 1})
    assert result["status"] == "REJECTED"
