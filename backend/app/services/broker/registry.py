"""Maps a broker catalog code (app.models.broker.Broker.code) to the adapter
class that implements BrokerInterface for it.

Four real adapters: delta_exchange (verified live, including real order
placement), zerodha_kite (written to Kite Connect v3's documented spec;
every endpoint's request/error format was confirmed live with placeholder
credentials, but full authenticated login was never exercised -- no
developer subscription available; see zerodha_broker.py's module
docstring), kotak_neo (wraps Kotak's own official Python SDK -- see
kotak_neo_broker.py's module docstring for exactly what that verifies),
and hdfc_securities (the least-verified of the four -- HDFC's docs are a
JS-rendered site that couldn't be read directly; see
hdfc_securities_broker.py's module docstring for a precise confirmed-vs-
inferred breakdown before relying on it for real capital).
"""

from app.services.broker.base import BrokerInterface
from app.services.broker.delta_broker import DeltaExchangeBroker
from app.services.broker.hdfc_securities_broker import HDFCSecuritiesBroker
from app.services.broker.kotak_neo_broker import KotakNeoBroker
from app.services.broker.mock_broker import MockBroker
from app.services.broker.zerodha_broker import ZerodhaKiteBroker

_REGISTRY: dict[str, type[BrokerInterface]] = {
    "zerodha_kite": ZerodhaKiteBroker,
    "delta_exchange": DeltaExchangeBroker,
    "hdfc_securities": HDFCSecuritiesBroker,
    "kotak_neo": KotakNeoBroker,
}

# Brokers whose auth can't complete in a single authenticate() call --
# they need an interactive browser login first (see the relevant
# adapter's module docstring for why). Kotak Neo is NOT here: TOTP+MPIN
# auth completes in one authenticate() call (a fresh TOTP code is derived
# from the stored secret each time), no browser redirect needed.
_INTERACTIVE_AUTH_BROKERS = {"zerodha_kite", "hdfc_securities"}


def get_broker_adapter(broker_code: str) -> BrokerInterface:
    adapter_cls = _REGISTRY.get(broker_code)
    if adapter_cls is None:
        raise ValueError(f"No broker adapter registered for code '{broker_code}'")
    return adapter_cls(broker_code)


def is_real_adapter(broker_code: str) -> bool:
    return _REGISTRY.get(broker_code) is not MockBroker


def requires_interactive_auth(broker_code: str) -> bool:
    return broker_code in _INTERACTIVE_AUTH_BROKERS
