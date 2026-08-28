import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.schemas.scanner import ScanCondition
from app.services.scanner import evaluate_condition, evaluate_field


def _candle(ts, close: float) -> OhlcvCandle:
    return OhlcvCandle(
        instrument_id=uuid.uuid4(), timeframe="1d", ts=ts, open=close, high=close + 1, low=close - 1,
        close=close, volume=1000.0, source="test",
    )


def _rising_candles(n=20):
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [_candle(base + timedelta(days=i), 100 + i) for i in range(n)]  # strictly rising -> RSI 100


def test_evaluate_field_raw_field():
    candles = _rising_candles(5)
    assert evaluate_field(candles, "close") == candles[-1].close


def test_evaluate_field_indicator_requires_dot_notation():
    with pytest.raises(ValueError):
        evaluate_field(_rising_candles(20), "rsi")  # must be "rsi.rsi"


def test_evaluate_field_indicator_dotted():
    value = evaluate_field(_rising_candles(20), "rsi.rsi")
    assert value == pytest.approx(100.0)


def test_evaluate_field_unknown_output_raises():
    with pytest.raises(ValueError):
        evaluate_field(_rising_candles(20), "rsi.nonexistent_output")


def test_evaluate_field_empty_candles_returns_none():
    assert evaluate_field([], "close") is None


def test_evaluate_condition_operators():
    candles = _rising_candles(20)
    passed, value = evaluate_condition(candles, ScanCondition(field="rsi.rsi", operator=">", value=90))
    assert passed is True
    assert value == pytest.approx(100.0)

    passed, _ = evaluate_condition(candles, ScanCondition(field="rsi.rsi", operator="<", value=50))
    assert passed is False


def test_evaluate_condition_insufficient_data_does_not_match():
    candles = _rising_candles(3)  # not enough for RSI(14)
    passed, value = evaluate_condition(candles, ScanCondition(field="rsi.rsi", operator=">", value=0))
    assert passed is False
    assert value is None


async def test_scanner_api_finds_matching_instrument(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = Instrument(
        exchange="NSE", symbol="RISING", name="Rising Co", instrument_type="equity",
        data_source="yahoo_nse", external_ref="RISING",
    )
    db_session.add(instrument)
    await db_session.flush()
    for c in _rising_candles(20):
        c.instrument_id = instrument.id
        db_session.add(c)
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login.json()["access_token"]

    resp = await client.post(
        "/api/v1/scanner/run",
        json={"exchange": "NSE", "timeframe": "1d", "conditions": [{"field": "rsi.rsi", "operator": ">", "value": 90}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any(m["instrument"]["symbol"] == "RISING" for m in body["matched"])


async def test_saved_scan_crud(client: AsyncClient, seeded_admin: dict):
    login = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    create_resp = await client.post(
        "/api/v1/scanner/saved",
        json={"name": "My Scan", "timeframe": "1d", "conditions": [{"field": "close", "operator": ">", "value": 0}]},
        headers=headers,
    )
    assert create_resp.status_code == 201
    scan_id = create_resp.json()["id"]

    list_resp = await client.get("/api/v1/scanner/saved", headers=headers)
    assert any(s["id"] == scan_id for s in list_resp.json())

    delete_resp = await client.delete(f"/api/v1/scanner/saved/{scan_id}", headers=headers)
    assert delete_resp.status_code == 204

    list_resp2 = await client.get("/api/v1/scanner/saved", headers=headers)
    assert not any(s["id"] == scan_id for s in list_resp2.json())
