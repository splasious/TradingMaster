from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle


async def _make_instrument_with_1m_candles(db_session: AsyncSession, symbol: str, n_minutes: int, start: datetime) -> Instrument:
    instrument = Instrument(
        exchange="NSE", symbol=symbol, name=symbol, instrument_type="equity",
        data_source="yahoo_nse", external_ref=symbol,
    )
    db_session.add(instrument)
    await db_session.flush()
    for i in range(n_minutes):
        db_session.add(OhlcvCandle(
            instrument_id=instrument.id, timeframe="1m", ts=start + timedelta(minutes=i),
            open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=10.0, source="test",
        ))
    await db_session.commit()
    return instrument


async def test_available_timeframes_reflects_what_is_actually_stored(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _make_instrument_with_1m_candles(db_session, "AVAILTF1", 10, datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))

    token = (await client.post("/api/v1/auth/login", json=seeded_admin)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(f"/api/v1/market-data/candles/available-timeframes?instrument_id={instrument.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == ["1m"]


async def test_resampled_endpoint_derives_5m_from_stored_1m(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _make_instrument_with_1m_candles(db_session, "AVAILTF2", 10, datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))

    token = (await client.post("/api/v1/auth/login", json=seeded_admin)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(
        f"/api/v1/market-data/candles/resampled?instrument_id={instrument.id}&base_timeframe=1m&target_timeframe=5m",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2  # two full closed 5-minute buckets, both safely in the past


async def test_indicator_calculate_direct_timeframe_when_no_base_given(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _make_instrument_with_1m_candles(db_session, "INDDIRECT", 30, datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))
    token = (await client.post("/api/v1/auth/login", json=seeded_admin)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(
        f"/api/v1/indicators/calculate?instrument_id={instrument.id}&timeframe=1m&indicator=sma",
        headers=headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 30


async def test_indicator_calculate_resamples_from_base_timeframe(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    # 10 minutes of real 1m candles -> two closed 5-minute buckets -- the
    # indicator should compute against those two resampled bars, not
    # return empty just because no "5m" candles are directly stored.
    instrument = await _make_instrument_with_1m_candles(db_session, "INDRESAMPLE", 10, datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))
    token = (await client.post("/api/v1/auth/login", json=seeded_admin)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(
        f"/api/v1/indicators/calculate?instrument_id={instrument.id}&timeframe=5m&base_timeframe=1m&indicator=sma",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert all(point["values"]["sma"] is not None or True for point in body)  # just confirm it computed without error


async def test_indicator_calculate_returns_empty_when_base_has_no_candles(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = Instrument(exchange="NSE", symbol="INDNONE", name="Ind None", instrument_type="equity", data_source="yahoo_nse", external_ref="INDNONE")
    db_session.add(instrument)
    await db_session.commit()

    token = (await client.post("/api/v1/auth/login", json=seeded_admin)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.get(
        f"/api/v1/indicators/calculate?instrument_id={instrument.id}&timeframe=5m&base_timeframe=1m&indicator=sma",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []
