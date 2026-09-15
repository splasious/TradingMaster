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
        data_source="zerodha_kite", external_ref="RISING",
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


async def _seed_instrument_with_candles(db_session: AsyncSession, symbol: str, base_price: float, n=40):
    instrument = Instrument(
        exchange="NSE", symbol=symbol, name=f"{symbol} Co", instrument_type="equity",
        data_source="zerodha_kite", external_ref=symbol,
    )
    db_session.add(instrument)
    await db_session.flush()
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(n):
        close = base_price + i * 0.5
        db_session.add(
            OhlcvCandle(
                instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5,
                high=close + 1, low=close - 1, close=close, volume=1000.0, source="test",
            )
        )
    return instrument


THRESHOLD_SCAN_CODE = (
    'def generate_signal(candles, params):\n'
    '    return "BUY" if candles[-1]["close"] > 150 else "HOLD"\n'
)


async def test_strategy_scan_finds_matching_signal_across_many_instruments(
    client: AsyncClient, seeded_admin: dict, db_session: AsyncSession
):
    """Exercises the batched market-scanner path (services/scanner.py::
    run_python_strategy_scan) across more instruments than fit in one
    PORTFOLIO_BATCH_SIZE-sized subprocess call -- this only passes if
    results from every batch get merged back correctly, and if HOLD
    signals are correctly excluded from the results."""
    from app.services.backtest.signals import PORTFOLIO_BATCH_SIZE

    n_instruments = PORTFOLIO_BATCH_SIZE + 5  # spans two batches
    above = []
    below = []
    for i in range(n_instruments):
        if i % 2 == 0:
            inst = await _seed_instrument_with_candles(db_session, f"SCANHI{i:03d}", base_price=140, n=40)  # ends above 150
            above.append(inst)
        else:
            inst = await _seed_instrument_with_candles(db_session, f"SCANLO{i:03d}", base_price=50, n=40)  # ends below 150
            below.append(inst)
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Threshold Scan Strategy",
            "version": {"python_code": THRESHOLD_SCAN_CODE, "parameters": {"threshold": 150.0, "rsi_period": 14.0}},
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post(
        "/api/v1/scanner/run-strategy",
        json={"strategy_id": strategy_id, "exchange": "NSE"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    matched_symbols = {m["instrument"]["symbol"] for m in body["matched"]}
    assert matched_symbols == {i.symbol for i in above}
    assert all(m["signal"] == "BUY" for m in body["matched"])
    assert body["strategy_version_number"] == 1
    assert body["parameters"] == {"threshold": 150.0, "rsi_period": 14.0}
    for inst in below:
        assert inst.symbol not in matched_symbols


async def test_strategy_scan_rejects_visual_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    await _seed_instrument_with_candles(db_session, "SCANVIS", 100)
    await db_session.commit()

    login = await client.post("/api/v1/auth/login", json=seeded_admin)
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Visual Only Scan",
            "version": {"entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]}, "exit_rules": {"all": []}},
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post("/api/v1/scanner/run-strategy", json={"strategy_id": strategy_id}, headers=headers)
    assert resp.status_code == 400


async def test_strategy_scan_rejects_non_owner(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "TraderScanPassX1!"
    other = User(email="traderscanx1@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader Scan")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    admin_login = await client.post("/api/v1/auth/login", json=seeded_admin)
    admin_token = admin_login.json()["access_token"]
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Owned Scan Strategy", "version": {"python_code": THRESHOLD_SCAN_CODE}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    strategy_id = strategy_resp.json()["id"]

    other_login = await client.post("/api/v1/auth/login", json={"email": "traderscanx1@tradingmaster.internal", "password": password})
    other_token = other_login.json()["access_token"]
    resp = await client.post(
        "/api/v1/scanner/run-strategy",
        json={"strategy_id": strategy_id},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


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
