from datetime import datetime, timedelta, timezone

import httpx

from app.services.market_data.base import Bar, MarketDataSource, MarketDataSourceError

# Delta Exchange India public REST API (no auth required for market data --
# verified live against https://api.india.delta.exchange). Candle history
# only accepts resolutions from this exact set (confirmed via the API's own
# validation error): 5s,1m,3m,5m,15m,30m,1h,2h,4h,6h,12h,1d,1w. It has no
# monthly resolution, so "1mo" is deliberately unsupported rather than guessed.
BASE_URL = "https://api.india.delta.exchange"

_RESOLUTION_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "1h",
    "1d": "1d",
    "1wk": "1w",
}


class DeltaExchangeDataSource(MarketDataSource):
    """Public market data only (products + candle history) -- no API key
    needed or used. Authenticated endpoints (balances, orders) are a
    separate, later-phase adapter gated by the risk engine and paper-trading
    approval workflow (PRD section 49), not wired up here.
    """

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")

    async def get_historical_data(
        self, external_ref: str, timeframe: str, start: datetime | None, end: datetime | None
    ) -> list[Bar]:
        resolution = _RESOLUTION_MAP.get(timeframe)
        if resolution is None:
            raise MarketDataSourceError(
                f"Delta Exchange does not support timeframe '{timeframe}' (supported: {sorted(_RESOLUTION_MAP)})"
            )

        end = end or datetime.now(timezone.utc)
        start = start or (end - timedelta(days=730))

        params = {
            "resolution": resolution,
            "symbol": external_ref,
            "start": str(int(start.timestamp())),
            "end": str(int(end.timestamp())),
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{self.base_url}/v2/history/candles", params=params)
        except httpx.ConnectError as exc:
            raise MarketDataSourceError("Could not reach Delta Exchange's API.") from exc
        except httpx.TimeoutException as exc:
            raise MarketDataSourceError("Delta Exchange API timed out.") from exc

        body = resp.json()
        if not body.get("success"):
            raise MarketDataSourceError(f"Delta Exchange API error: {body.get('error')}")

        return [
            Bar(
                ts=datetime.fromtimestamp(row["time"], tz=timezone.utc),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row.get("volume"),
            )
            for row in body["result"]
        ]

    async def get_ticker(self, external_ref: str) -> dict:
        """Real current price + Delta's numeric product_id for one symbol
        (public endpoint, verified live: GET /v2/tickers/{symbol}) -- this
        is what live trading uses to decide whether to place a real order,
        never the simulated tick engine (Phase 2's tick_engine.py is for
        the Markets page display and paper trading only)."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self.base_url}/v2/tickers/{external_ref}")
        except httpx.ConnectError as exc:
            raise MarketDataSourceError("Could not reach Delta Exchange's API.") from exc
        except httpx.TimeoutException as exc:
            raise MarketDataSourceError("Delta Exchange API timed out.") from exc

        body = resp.json()
        if not body.get("success"):
            raise MarketDataSourceError(f"Delta Exchange API error: {body.get('error')}")
        result = body["result"]
        return {"price": float(result["close"]), "product_id": result["product_id"], "mark_price": float(result["mark_price"])}

    async def list_products(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.base_url}/v2/products",
                    params={"contract_types": "perpetual_futures", "states": "live"},
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MarketDataSourceError("Could not reach Delta Exchange's API.") from exc
        body = resp.json()
        if not body.get("success"):
            raise MarketDataSourceError(f"Delta Exchange API error: {body.get('error')}")
        return body["result"]
