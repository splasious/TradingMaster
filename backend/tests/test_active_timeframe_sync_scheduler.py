import json
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.services.broker.zerodha_broker import ZerodhaKiteBroker
from app.services.market_data.active_timeframe_sync_scheduler import ActiveTimeframeSyncScheduler, note_native_candle_demand

# A real, known NSE trading Thursday, well inside market hours (09:15-15:30
# IST == 03:45-10:00 UTC) -- same reference point test_market_data_freshness.py
# uses, so a zerodha-sourced pair's sync isn't silently skipped by the
# market-hours gate just because the test suite happens to run over a
# weekend or after-hours.
ZERODHA_MARKET_OPEN_NOW = datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc)


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


async def _active_zerodha_deployment_on(db: AsyncSession, instrument: Instrument, *, timeframe: str) -> PaperDeployment:
    owner_id = uuid.uuid4()
    strategy = Strategy(name="ATS Zerodha Test Strategy", owner_id=owner_id, code_type="python")
    db.add(strategy)
    await db.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe=timeframe,
        entry_rules={"all": []}, exit_rules={"all": []}, risk_rules={}, position_sizing={"type": "fixed_quantity", "value": 1},
        instrument_ids=[str(instrument.id)],
    )
    db.add(version)
    await db.flush()

    portfolio = PaperPortfolio(user_id=owner_id, name="ATS Zerodha Pool", currency="INR", cash=10000.0, initial_capital=10000.0)
    db.add(portfolio)
    await db.flush()

    deployment = PaperDeployment(
        strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        portfolio_id=portfolio.id, timeframe=timeframe, status=DeploymentStatus.ACTIVE.value,
    )
    db.add(deployment)
    await db.commit()
    return deployment


async def _active_zerodha_deployment(db: AsyncSession, *, timeframe: str) -> tuple[Instrument, PaperDeployment]:
    instrument = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE", expiry=date(2026, 9, 24), strike=23000, option_type="CE",
    )
    db.add(instrument)
    await db.flush()
    deployment = await _active_zerodha_deployment_on(db, instrument, timeframe=timeframe)
    return instrument, deployment


async def _seed_connected_zerodha_account(db: AsyncSession) -> None:
    from app.models.user import Role, User, UserRole

    role = Role(id=uuid.uuid4(), name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db.add(role)
    await db.flush()
    user = User(email=f"ats_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="ATS Test")
    user.user_roles = [UserRole(role=role)]
    db.add(user)
    await db.flush()
    broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
    db.add(broker)
    await db.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="ATS Test", environment="paper")
    db.add(account)
    await db.flush()
    creds = {"api_key": "kitekey", "api_secret": "kitesecret", "access_token": "tok"}
    db.add(BrokerCredential(broker_account_id=account.id, encrypted_payload=encrypt_payload(json.dumps(creds))))
    db.add(BrokerConnection(broker_account_id=account.id, status=ConnectionStatus.CONNECTED.value))
    await db.commit()


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


async def test_sync_corrects_existing_candles_instead_of_duplicating(db_session: AsyncSession, monkeypatch):
    """A stored candle is rewritten with the source's values, not skipped --
    before, a candle first stored while still forming kept that partial
    close forever."""
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
    assert candles[0].close == 10.5  # corrected to the source's value


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


async def test_sync_fetches_zerodha_sourced_pairs_including_open_interest(db_session: AsyncSession, monkeypatch):
    instrument, _ = await _active_zerodha_deployment(db_session, timeframe="15m")
    await _seed_connected_zerodha_account(db_session)
    bar_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    captured_segment = []

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        captured_segment.append(segment)
        assert symbol == "NIFTY26SEP23000CE"
        return [{"ts": bar_ts, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10, "open_interest": 5000}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW)
    assert synced == 1
    assert captured_segment == ["NFO"]  # from instrument.exchange, not guessed

    candles = (
        await db_session.execute(
            select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == "15m")
        )
    ).scalars().all()
    assert len(candles) == 1
    assert candles[0].close == 100.5
    assert candles[0].open_interest == 5000


async def test_sync_skips_zerodha_pairs_when_market_closed(db_session: AsyncSession, monkeypatch):
    """No Kite historical-data call at all while NSE is shut -- there's
    nothing new to fetch, and a narrow "last couple of days" window would
    otherwise just re-confirm there's no candle for a day the exchange
    never opened (see the incident this was built to prevent: a native
    strategy trading a whole Saturday against Friday's frozen price)."""
    await _active_zerodha_deployment(db_session, timeframe="15m")
    await _seed_connected_zerodha_account(db_session)

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        raise AssertionError("get_historical_data must not be called while the market is closed")

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    market_closed_now = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)  # a Saturday
    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session, now=market_closed_now)
    assert synced == 0


async def test_sync_skips_zerodha_pairs_without_a_connected_account(db_session: AsyncSession, monkeypatch):
    await _active_zerodha_deployment(db_session, timeframe="15m")
    # No connected Zerodha account seeded this time.

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        raise AssertionError("get_historical_data must not be called with no connected Zerodha account")

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW)
    assert synced == 0


async def test_sync_fetches_1wk_zerodha_pair_as_1d_with_wide_backfill_window(db_session: AsyncSession, monkeypatch):
    """Kite has no native weekly interval -- a "1wk" pair must fetch and
    store "1d" instead, and (with zero existing daily history) use the
    wide one-time backfill window, not the normal 2-day incremental one."""
    instrument, _ = await _active_zerodha_deployment(db_session, timeframe="1wk")
    await _seed_connected_zerodha_account(db_session)
    bar_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    captured = {}

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        captured["timeframe"] = timeframe
        captured["window_days"] = (end - start).days
        return [{"ts": bar_ts, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW)
    assert synced == 1
    assert captured["timeframe"] == "1d"  # not "1wk" -- Kite doesn't support it
    assert captured["window_days"] > 300  # wide one-time backfill, not the normal 2-day window

    candles = (
        await db_session.execute(
            select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id)
        )
    ).scalars().all()
    assert len(candles) == 1
    assert candles[0].timeframe == "1d"  # stored as "1d", not "1wk"


async def test_sync_uses_narrow_lookback_once_enough_1d_bars_exist(db_session: AsyncSession, monkeypatch):
    instrument, _ = await _active_zerodha_deployment(db_session, timeframe="1wk")
    await _seed_connected_zerodha_account(db_session)
    base_ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(95):
        db_session.add(OhlcvCandle(
            instrument_id=instrument.id, timeframe="1d", ts=base_ts + timedelta(days=i),
            open=1, high=1, low=1, close=1, volume=1, source="zerodha_kite",
        ))
    await db_session.commit()

    captured = {}

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        captured["window_days"] = (end - start).days
        return []

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    scheduler = ActiveTimeframeSyncScheduler()
    await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW)
    assert captured["window_days"] <= 2  # enough daily history already -- back to the normal incremental window


async def test_sync_dedupes_1wk_and_1mo_fetches_for_the_same_instrument(db_session: AsyncSession, monkeypatch):
    instrument, _ = await _active_zerodha_deployment(db_session, timeframe="1wk")
    await _active_zerodha_deployment_on(db_session, instrument, timeframe="1mo")
    await _seed_connected_zerodha_account(db_session)

    call_count = {"n": 0}

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        call_count["n"] += 1
        return [{"ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    scheduler = ActiveTimeframeSyncScheduler()
    synced = await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW)
    assert call_count["n"] == 1  # both "1wk" and "1mo" map to the same "1d" fetch -- only done once
    assert synced == 1


async def _nse_equity(db: AsyncSession, symbol: str = "SOLARINDS") -> Instrument:
    instrument = Instrument(exchange="NSE", symbol=symbol, name=symbol, instrument_type="equity", data_source="zerodha_kite", external_ref=symbol)
    db.add(instrument)
    await db.commit()
    return instrument


async def _stored_15m(db: AsyncSession, instrument: Instrument) -> list[OhlcvCandle]:
    return list((
        await db.execute(
            select(OhlcvCandle).where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == "15m").order_by(OhlcvCandle.ts)
        )
    ).scalars().all())


async def test_sync_never_stores_the_still_forming_candle(db_session: AsyncSession, monkeypatch):
    """Kite returns the current candle too -- stored, it would keep its
    close from whatever second the sync saw it."""
    instrument, _ = await _active_zerodha_deployment(db_session, timeframe="15m")
    await _seed_connected_zerodha_account(db_session)
    finished = ZERODHA_MARKET_OPEN_NOW - timedelta(minutes=20)  # 08:10-08:25 UTC, closed
    forming = ZERODHA_MARKET_OPEN_NOW - timedelta(minutes=5)  # 08:25-08:40 UTC, still open

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        return [
            {"ts": finished, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
            {"ts": forming, "open": 100.5, "high": 100.5, "low": 100.5, "close": 100.5, "volume": 1},
        ]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    await ActiveTimeframeSyncScheduler().sync(db_session, now=ZERODHA_MARKET_OPEN_NOW)

    candles = await _stored_15m(db_session, instrument)
    assert [c.ts.replace(tzinfo=timezone.utc) for c in candles] == [finished]


async def test_sync_covers_pairs_a_native_strategy_reads(db_session: AsyncSession, monkeypatch):
    """An Advanced deployment's stocks live only in its own state -- no
    deployment row names them. ctx.get_candles() records them instead."""
    instrument = await _nse_equity(db_session)
    await _seed_connected_zerodha_account(db_session)
    bar_ts = ZERODHA_MARKET_OPEN_NOW - timedelta(minutes=30)
    captured = []

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        captured.append((symbol, timeframe, segment))
        return [{"ts": bar_ts, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    scheduler = ActiveTimeframeSyncScheduler()
    assert await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW) == 0  # nobody's asked for it yet

    note_native_candle_demand(instrument.id, "15m", ZERODHA_MARKET_OPEN_NOW)
    assert await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW) == 1
    assert captured == [("SOLARINDS", "15m", "NSE")]
    assert len(await _stored_15m(db_session, instrument)) == 1

    # A strategy that stops asking (stopped, or changed its watchlist) drops off.
    later = ZERODHA_MARKET_OPEN_NOW + timedelta(hours=1)
    assert await ActiveTimeframeSyncScheduler().sync(db_session, now=later) == 0


async def test_sync_waits_for_the_next_candle_to_finish_before_fetching_again(db_session: AsyncSession, monkeypatch):
    await _active_zerodha_deployment(db_session, timeframe="15m")
    await _seed_connected_zerodha_account(db_session)
    newest = ZERODHA_MARKET_OPEN_NOW - timedelta(minutes=15)  # 08:15-08:30 UTC, just closed
    calls = {"n": 0}

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        calls["n"] += 1
        return [{"ts": newest, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    scheduler = ActiveTimeframeSyncScheduler()
    await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW + timedelta(seconds=10))  # 08:15 candle closed and settled
    assert calls["n"] == 1
    await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW + timedelta(minutes=14))  # 08:30-08:45 still forming
    assert calls["n"] == 1
    await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW + timedelta(minutes=15, seconds=5))  # closed, not settled
    assert calls["n"] == 1
    await scheduler.sync(db_session, now=ZERODHA_MARKET_OPEN_NOW + timedelta(minutes=15, seconds=10))  # final now
    assert calls["n"] == 2


async def test_sync_catches_up_a_pair_that_fell_days_behind(db_session: AsyncSession, monkeypatch):
    instrument, _ = await _active_zerodha_deployment(db_session, timeframe="15m")
    await _seed_connected_zerodha_account(db_session)
    last_stored = ZERODHA_MARKET_OPEN_NOW - timedelta(days=6)
    db_session.add(OhlcvCandle(
        instrument_id=instrument.id, timeframe="15m", ts=last_stored, open=1, high=1, low=1, close=1, volume=1, source="zerodha_kite",
    ))
    await db_session.commit()
    captured = {}

    async def fake_get_historical_data(self, symbol, timeframe, start, end, segment="NSE"):
        captured["start"] = start
        return []

    monkeypatch.setattr(ZerodhaKiteBroker, "get_historical_data", fake_get_historical_data)

    await ActiveTimeframeSyncScheduler().sync(db_session, now=ZERODHA_MARKET_OPEN_NOW)
    assert captured["start"] <= last_stored  # the whole gap, not just the last 2 days
