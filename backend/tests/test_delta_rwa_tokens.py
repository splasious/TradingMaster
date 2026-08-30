import httpx
import pytest

from app.services.market_data.delta_source import DeltaExchangeDataSource

_SAMPLE_PRODUCTS = [
    {"symbol": "BTCUSD", "description": "Bitcoin Perpetual futures, quoted, settled & margined in US Dollar"},
    {"symbol": "ETHUSD", "description": "Ethereum Perpetual futures, quoted, settled & margined in US Dollar"},
    {"symbol": "NVDAXUSD", "description": "NVIDIA xStock Token perpetual future quoted in USD"},
    {"symbol": "PLTRBUSD", "description": "Palantir Technologies bStocks Token perpetual future quoted in USD"},
    {"symbol": "DOGEUSD", "description": "Dogecoin perpetual future quoted in USD"},
]


def _mock_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "http://test"))


async def test_list_rwa_token_products_excludes_crypto(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return _mock_response({"success": True, "result": _SAMPLE_PRODUCTS})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    source = DeltaExchangeDataSource()
    rwa = await source.list_rwa_token_products()

    symbols = {p["symbol"] for p in rwa}
    assert symbols == {"NVDAXUSD", "PLTRBUSD"}
    assert "BTCUSD" not in symbols
    assert "DOGEUSD" not in symbols


async def test_list_rwa_token_products_empty_when_none_match(monkeypatch):
    async def fake_get(self, url, **kwargs):
        return _mock_response({"success": True, "result": [{"symbol": "BTCUSD", "description": "Bitcoin Perpetual"}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    source = DeltaExchangeDataSource()
    rwa = await source.list_rwa_token_products()
    assert rwa == []


def test_4h_resolution_is_supported():
    from app.services.market_data.delta_source import _RESOLUTION_MAP

    assert _RESOLUTION_MAP["4h"] == "4h"
