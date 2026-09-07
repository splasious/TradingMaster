from app.services.market_data.base import MarketDataSource
from app.services.market_data.delta_source import DeltaExchangeDataSource

_REGISTRY: dict[str, type[MarketDataSource]] = {
    "delta_exchange": DeltaExchangeDataSource,
}


def get_market_data_source(data_source: str) -> MarketDataSource:
    adapter_cls = _REGISTRY.get(data_source)
    if adapter_cls is None:
        raise ValueError(f"No market data source registered for '{data_source}'")
    return adapter_cls()
