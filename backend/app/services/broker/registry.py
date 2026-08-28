"""Maps a broker catalog code (app.models.broker.Broker.code) to the adapter
class that implements BrokerInterface for it.

Real adapters (ZerodhaKiteBroker, DeltaExchangeBroker) get registered here in
a later phase without any change to the code that calls this registry.
"""

from app.services.broker.base import BrokerInterface
from app.services.broker.mock_broker import MockBroker

_REGISTRY: dict[str, type[BrokerInterface]] = {
    "zerodha_kite": MockBroker,
    "delta_exchange": MockBroker,
}


def get_broker_adapter(broker_code: str) -> BrokerInterface:
    adapter_cls = _REGISTRY.get(broker_code)
    if adapter_cls is None:
        raise ValueError(f"No broker adapter registered for code '{broker_code}'")
    return adapter_cls(broker_code)


def is_real_adapter(broker_code: str) -> bool:
    return _REGISTRY.get(broker_code) is not MockBroker
