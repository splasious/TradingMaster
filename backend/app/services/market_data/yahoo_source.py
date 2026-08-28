from datetime import datetime, timezone

import httpx

from app.core.config import get_settings
from app.services.market_data.base import Bar, MarketDataSource, MarketDataSourceError

settings = get_settings()


class YahooNSEDataSource(MarketDataSource):
    """Calls the local nse-yahoo-data service's /ohlcv endpoint -- real NSE
    equity/index history scraped from Yahoo Finance, already relied on by
    this user's other trading projects. Not bundled with TradingMaster;
    if it isn't running, callers get a clear MarketDataSourceError rather
    than a raw connection-refused traceback.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.yahoo_data_service_url).rstrip("/")

    async def get_historical_data(
        self, external_ref: str, timeframe: str, start: datetime | None, end: datetime | None
    ) -> list[Bar]:
        params: dict[str, str] = {"symbol": external_ref, "interval": timeframe}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(f"{self.base_url}/ohlcv", params=params)
        except httpx.ConnectError as exc:
            raise MarketDataSourceError(
                "Could not reach the nse-yahoo-data service. Is it running on "
                f"{self.base_url}? (Start it with `python app/main.py` in that repo.)"
            ) from exc
        except httpx.TimeoutException as exc:
            raise MarketDataSourceError("nse-yahoo-data service timed out.") from exc

        if resp.status_code == 404:
            return []
        if resp.status_code >= 400:
            raise MarketDataSourceError(f"nse-yahoo-data returned {resp.status_code}: {resp.text[:300]}")

        bars: list[Bar] = []
        for row in resp.json():
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            bars.append(
                Bar(
                    ts=ts,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row.get("volume"),
                )
            )
        return bars

    async def list_symbols(self) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(f"{self.base_url}/symbols")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise MarketDataSourceError(
                f"Could not reach the nse-yahoo-data service on {self.base_url}."
            ) from exc
        if resp.status_code >= 400:
            raise MarketDataSourceError(f"nse-yahoo-data returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()
