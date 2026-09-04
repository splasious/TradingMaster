import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.services.market_data.active_timeframe_sync_scheduler import ActiveTimeframeSyncScheduler


def _delta_response(bars: list[dict]) -> httpx.Response:
    payload = {
        "success": True,
        "result": [
            {"time": int(b["ts"].timestamp()), "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"], "volume": b.get("volume", 0)}
            for b in bars
        ],
    }
    return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", "https://example.com"))


async def _active_deployment(db: AsyncSession, *, timeframe: str) -> tuple[Instrument, PaperDeployment]:
    instrument = Instrument(
        exchange="DELTA", symbol="ATSXUSD", name="Active Timeframe Sync Co",
        instrument_type="perpetual_future", data_source="delta_exchange", external_ref="ATSXUSD",
    )
    db.add(instrument)
    await db.flush()

    owner_id = uuid.uuid4()
    strategy = Strategy(name="ATS Test Strategy", owner_id=owner_id, code_type="python")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe=timeframe,
        entry_rules={"all": []}, exit_rules={"all": []}, risk_rules={}, position_sizing={"type": "fixed_quantity", "value": 1},
        instrument_ids=[str(instrument.id)],
    )
    db.add(version)
    await db.flush()

    portfolio = PaperPortfolio(user_id=owner_id, name="ATS Pool", currency="USD", cash=10000.0, initial_capital=10000.0)
    db.add(portfolio)
    await db.flush()

    deployment = PaperDeployment(
        strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        portfolio_id=portfolio.id, timeframe=timeframe, status=DeploymentStatus.ACTIVE.value,
    )
    db.add(deployment)
    await db.commit()
    return instrument, deployment


async def test_sync_fetches_the_timeframe_an_active_deployment_actually_uses(db_session: AsyncSession, monkeypatch):
    instrument, _ = await _active_deployment(db_session, timeframe="15m")
    bar_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    captured_intervals: list[str] = []

    async def fake_get(client_self, url, **kwargs):
        params = kwargs.get("params", {})
        captured_intervals.append(params.get("resolution", ""))
        return _delta_response([{"ts": bar_ts, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 5}])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session)
    assert synced == 1

    candles = (
        await db_session.execute(
            select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == "15m")
        )
    ).scalars().all()
    assert len(candles) == 1
    assert candles[0].close == 10.5


async def test_sync_does_not_duplicate_existing_candles(db_session: AsyncSession, monkeypatch):
    instrument, _ = await _active_deployment(db_session, timeframe="15m")
    bar_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add(
        OhlcvCandle(
            instrument_id=instrument.id, timeframe="15m", ts=bar_ts,
            open=1, high=1, low=1, close=1, volume=1, source="delta_exchange",
        )
    )
    await db_session.commit()

    async def fake_get(client_self, url, **kwargs):
        return _delta_response([{"ts": bar_ts, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 5}])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    scheduler = ActiveTimeframeSyncScheduler()
    await scheduler.sync(db_session)

    candles = (
        await db_session.execute(
            select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == "15m")
        )
    ).scalars().all()
    assert len(candles) == 1  # still just the one -- not duplicated
    assert candles[0].close == 1  # untouched, not overwritten


async def test_sync_ignores_instruments_with_no_active_deployment(db_session: AsyncSession, monkeypatch):
    idle_instrument = Instrument(
        exchange="DELTA", symbol="IDLEXUSD", name="Idle Co",
        instrument_type="perpetual_future", data_source="delta_exchange", external_ref="IDLEXUSD",
    )
    db_session.add(idle_instrument)
    await db_session.commit()

    call_count = 0

    async def fake_get(client_self, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return _delta_response([])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session)
    assert synced == 0
    assert call_count == 0  # nothing to sync -- no deployments at all, so no API calls made


async def test_sync_ignores_stopped_deployments(db_session: AsyncSession, monkeypatch):
    instrument, deployment = await _active_deployment(db_session, timeframe="15m")
    deployment.status = DeploymentStatus.STOPPED.value
    await db_session.commit()

    async def fake_get(client_self, url, **kwargs):
        return _delta_response([{"ts": datetime.now(timezone.utc), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session)
    assert synced == 0
