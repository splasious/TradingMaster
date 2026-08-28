import inspect

import pytest

from app.services.broker.base import BrokerInterface
from app.services.broker.mock_broker import MockBroker
from app.services.broker.registry import get_broker_adapter, is_real_adapter


def test_mock_broker_implements_full_interface():
    interface_methods = {name for name, _ in inspect.getmembers(BrokerInterface, predicate=inspect.isfunction)}
    mock = MockBroker("zerodha_kite")
    for name in interface_methods:
        assert hasattr(mock, name)
        assert inspect.iscoroutinefunction(getattr(mock, name))


def test_registry_returns_broker_interface_instances():
    for code in ("zerodha_kite", "delta_exchange"):
        adapter = get_broker_adapter(code)
        assert isinstance(adapter, BrokerInterface)
        assert is_real_adapter(code) is False  # Phase 1: stubbed only


def test_registry_rejects_unknown_broker():
    with pytest.raises(ValueError):
        get_broker_adapter("some_unregistered_broker")


async def test_mock_broker_never_places_a_real_order():
    mock = MockBroker("zerodha_kite")
    await mock.connect()
    result = await mock.place_order({"symbol": "NIFTY", "side": "BUY", "quantity": 1})
    assert result["status"] == "REJECTED"
