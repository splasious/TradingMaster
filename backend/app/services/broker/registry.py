"""Maps a broker catalog code (app.models.broker.Broker.code) to the adapter
class that implements BrokerInterface for it.

zerodha_kite is still a MockBroker -- no real Zerodha adapter has been
built. delta_exchange is now the real adapter (Phase 7): it places actual
orders with actual money once a user connects real credentials through
Settings > Brokers.
"""

from app.services.broker.base import BrokerInterface
from app.services.broker.delta_broker import DeltaExchangeBroker
from app.services.broker.mock_broker import MockBroker

_REGISTRY: dict[str, type[BrokerInterface]] = {
    "zerodha_kite": MockBroker,
    "delta_exchange": DeltaExchangeBroker,
}


def get_broker_adapter(broker_code: str) -> BrokerInterface:
    adapter_cls = _REGISTRY.get(broker_code)
    if adapter_cls is None:
        raise ValueError(f"No broker adapter registered for code '{broker_code}'")
    return adapter_cls(broker_code)


def is_real_adapter(broker_code: str) -> bool:
    return _REGISTRY.get(broker_code) is not MockBroker
