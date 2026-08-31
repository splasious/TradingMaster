from datetime import datetime, time, timezone

import httpx

from app.core.config import get_settings
from app.services.market_data.base import Bar, MarketDataSource, MarketDataSourceError

settings = get_settings()

# The nse-yahoo-data service's own storage doesn't always return the same
# time-of-day for a given trading day's daily bar across different fetches
# (depends on whether the underlying yfinance/pandas call produced a
# tz-aware or tz-naive index at ingest time -- e.g. 2026-08-13 04:00 UTC one
# run, 2026-08-13 00:00 UTC another). Two TradingMaster ingestion paths
# (the main-catalog backfill and the Data Backfill Platform) hit this same
# service independently, so without normalizing here both timestamps land
# as distinct rows for the same calendar day -- two candles for one day.
# Daily-and-coarser bars only ever need calendar-day resolution, so collapse
# to a canonical midnight UTC per day; that makes the (instrument, timeframe,
# ts) uniqueness check in both ingestion paths actually dedupe by day.
_DAY_GRANULAR_TIMEFRAMES = {"1d", "1wk", "1mo"}


def _normalize_ts(ts: datetime, timeframe: str) -> datetime:
    if timeframe in _DAY_GRANULAR_TIMEFRAMES:
        return datetime.combine(ts.date(), time.min, tzinfo=timezone.utc)
    return ts


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
            ts = _normalize_ts(ts, timeframe)
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
