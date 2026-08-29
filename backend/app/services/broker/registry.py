"""Maps a broker catalog code (app.models.broker.Broker.code) to the adapter
class that implements BrokerInterface for it.

Both brokers now have real adapters: delta_exchange (verified live,
including real order placement) and zerodha_kite (written to Kite Connect
v3's documented spec; every endpoint's request/error format was confirmed
live with placeholder credentials, but full authenticated login was never
exercised -- no developer subscription available; see zerodha_broker.py's
module docstring).
"""

from app.services.broker.base import BrokerInterface
from app.services.broker.delta_broker import DeltaExchangeBroker
from app.services.broker.mock_broker import MockBroker
from app.services.broker.zerodha_broker import ZerodhaKiteBroker

_REGISTRY: dict[str, type[BrokerInterface]] = {
    "zerodha_kite": ZerodhaKiteBroker,
    "delta_exchange": DeltaExchangeBroker,
}

# Brokers whose auth can't complete in a single authenticate() call --
# they need an interactive browser login first (see the relevant
# adapter's module docstring for why).
_INTERACTIVE_AUTH_BROKERS = {"zerodha_kite"}


def get_broker_adapter(broker_code: str) -> BrokerInterface:
    adapter_cls = _REGISTRY.get(broker_code)
    if adapter_cls is None:
        raise ValueError(f"No broker adapter registered for code '{broker_code}'")
    return adapter_cls(broker_code)


def is_real_adapter(broker_code: str) -> bool:
    return _REGISTRY.get(broker_code) is not MockBroker


def requires_interactive_auth(broker_code: str) -> bool:
    return broker_code in _INTERACTIVE_AUTH_BROKERS
