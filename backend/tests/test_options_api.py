from datetime import date, datetime, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle

EXPIRY = date(2026, 9, 15)


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed(db_session: AsyncSession):
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.flush()
    ce = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000CE", name="NIFTY26SEP23000CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000CE", expiry=EXPIRY, strike=23000, option_type="CE",
        lot_size=65, underlying_instrument_id=underlying.id,
    )
    pe = Instrument(
        exchange="NFO", symbol="NIFTY26SEP23000PE", name="NIFTY26SEP23000PE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY26SEP23000PE", expiry=EXPIRY, strike=23000, option_type="PE",
        lot_size=65, underlying_instrument_id=underlying.id,
    )
    db_session.add_all([ce, pe])
    await db_session.flush()
    ts = datetime(2026, 9, 1, 9, 15, tzinfo=timezone.utc)
    db_session.add(OhlcvCandle(instrument_id=ce.id, timeframe="15m", ts=ts, open=100, high=101, low=99, close=100, volume=10, open_interest=1000, source="test"))
    db_session.add(OhlcvCandle(instrument_id=pe.id, timeframe="15m", ts=ts, open=50, high=51, low=49, close=50, volume=10, open_interest=2000, source="test"))
    await db_session.commit()
    return underlying


async def test_options_dashboard_endpoints_end_to_end(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    underlying = await _seed(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    underlyings_resp = await client.get("/api/v1/options/underlyings", headers=headers)
    assert underlyings_resp.status_code == 200
    assert any(u["instrument_id"] == str(underlying.id) for u in underlyings_resp.json())

    expiries_resp = await client.get(f"/api/v1/options/{underlying.id}/expiries", headers=headers)
    assert expiries_resp.status_code == 200
    assert expiries_resp.json() == [{"expiry": "2026-09-15", "future_count": 0, "option_count": 2}]

    pcr_resp = await client.get(f"/api/v1/options/{underlying.id}/pcr", params={"expiry": "2026-09-15", "timeframe": "15m"}, headers=headers)
    assert pcr_resp.status_code == 200
    points = pcr_resp.json()
    assert len(points) == 1
    assert points[0]["total_call_oi"] == 1000
    assert points[0]["total_put_oi"] == 2000
    assert points[0]["pcr"] == 2.0

    chain_resp = await client.get(f"/api/v1/options/{underlying.id}/chain", params={"expiry": "2026-09-15"}, headers=headers)
    assert chain_resp.status_code == 200
    rows = chain_resp.json()
    assert len(rows) == 1
    assert rows[0]["strike"] == 23000
    assert rows[0]["call"]["ltp"] == 100
    assert rows[0]["call"]["open_interest"] == 1000
    assert rows[0]["call"]["ltp_change"] == 0  # only one candle -- day-open == latest
    assert rows[0]["put"]["ltp"] == 50
    assert rows[0]["put"]["open_interest"] == 2000
