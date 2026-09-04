import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.user import Role, User, UserRole


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed_instrument_with_candles(db_session: AsyncSession, n=60) -> Instrument:
    instrument = Instrument(
        exchange="NSE", symbol="BTQ", name="Backtest Co", instrument_type="equity",
        data_source="yahoo_nse", external_ref="BTQ",
    )
    db_session.add(instrument)
    await db_session.flush()
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(n):
        # a gentle uptrend with noise so both wins and losses occur
        close = 100 + i * 0.8 + (3 if i % 7 == 0 else 0) - (2 if i % 5 == 0 else 0)
        db_session.add(
            OhlcvCandle(
                instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5,
                high=close + 1, low=close - 1.5, close=close, volume=1000.0, source="test",
            )
        )
    await db_session.commit()
    return instrument


async def test_full_backtest_flow_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Backtest Target",
            "version": {
                "entry_rules": {"all": [{"field": "rsi.rsi", "operator": ">", "value": 40}]},
                "exit_rules": {"all": [{"field": "rsi.rsi", "operator": "<", "value": 30}]},
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    assert strategy_resp.json()["status"] == "draft"

    backtest_resp = await client.post(
        "/api/v1/backtests",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "timeframe": "1d", "initial_capital": 100000, "run_monte_carlo": True},
        headers=headers,
    )
    assert backtest_resp.status_code == 202
    job_id = backtest_resp.json()["id"]
    assert backtest_resp.json()["status"] == "pending"

    # BackgroundTasks run synchronously within the ASGI test transport, so
    # by the time the POST above returned, the job has already executed.
    job_resp = await client.get(f"/api/v1/backtests/{job_id}", headers=headers)
    assert job_resp.json()["status"] == "completed", job_resp.json()

    result_resp = await client.get(f"/api/v1/backtests/{job_id}/result", headers=headers)
    assert result_resp.status_code == 200
    result = result_resp.json()
    assert "net_profit" in result["metrics"]
    assert "sharpe_ratio" in result["metrics"]
    assert result["monte_carlo"] is not None
    assert len(result["equity_curve"]) == 60

    trades_resp = await client.get(f"/api/v1/backtests/{job_id}/trades", headers=headers)
    assert trades_resp.status_code == 200

    strategy_after = await client.get(f"/api/v1/strategies/{strategy_id}", headers=headers)
    assert strategy_after.json()["status"] == "backtested"


async def test_backtest_with_out_of_sample_split(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session, n=100)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "OOS Strategy",
            "version": {
                "entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]},
                "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]},
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    backtest_resp = await client.post(
        "/api/v1/backtests",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "timeframe": "1d", "out_of_sample_split_pct": 70},
        headers=headers,
    )
    job_id = backtest_resp.json()["id"]
    result = (await client.get(f"/api/v1/backtests/{job_id}/result", headers=headers)).json()
    assert result["out_of_sample_metrics"] is not None


async def test_backtest_date_range_filters_candles(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session, n=100)  # 2026-01-05 .. 2026-04-14
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Date Range Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    # Only the first 40 of the 100 seeded days fall in this window.
    backtest_resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_id": strategy_id, "instrument_id": str(instrument.id), "timeframe": "1d",
            "start_date": "2026-01-05", "end_date": "2026-02-13",
        },
        headers=headers,
    )
    job_id = backtest_resp.json()["id"]
    assert backtest_resp.json()["start_date"] == "2026-01-05"
    assert backtest_resp.json()["end_date"] == "2026-02-13"

    result = (await client.get(f"/api/v1/backtests/{job_id}/result", headers=headers)).json()
    assert len(result["equity_curve"]) == 40


async def test_backtest_rejects_start_after_end_date(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Bad Range", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_id": strategy_id, "instrument_id": str(instrument.id),
            "start_date": "2026-06-01", "end_date": "2026-01-01",
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_backtest_position_sizing_override(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Sizing Override Strategy",
            "version": {
                "entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]},
                "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]},
                "position_sizing": {"type": "fixed_quantity", "value": 1},
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    # Override to 10 units per trade instead of the strategy's baked-in 1.
    backtest_resp = await client.post(
        "/api/v1/backtests",
        json={
            "strategy_id": strategy_id, "instrument_id": str(instrument.id),
            "position_sizing_type": "fixed_quantity", "position_sizing_value": 10,
        },
        headers=headers,
    )
    job_id = backtest_resp.json()["id"]
    trades = (await client.get(f"/api/v1/backtests/{job_id}/trades", headers=headers)).json()
    assert len(trades) > 0
    assert all(t["quantity"] == 10 for t in trades)


async def test_backtest_sizing_override_requires_both_fields(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Partial Sizing", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post(
        "/api/v1/backtests",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "position_sizing_value": 5},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_non_owner_cannot_start_backtest(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session)
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "TraderPassX1!"
    other = User(email="traderx1@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader X")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    admin_token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Owned", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    strategy_id = strategy_resp.json()["id"]

    other_token = await _login(client, "traderx1@tradingmaster.internal", password)
    resp = await client.post(
        "/api/v1/backtests",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id)},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


async def test_backtest_fails_gracefully_with_insufficient_candles(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = Instrument(exchange="NSE", symbol="THIN", name="Thin Co", instrument_type="equity", data_source="yahoo_nse", external_ref="THIN")
    db_session.add(instrument)
    await db_session.flush()
    for i in range(5):
        db_session.add(
            OhlcvCandle(
                instrument_id=instrument.id, timeframe="1d", ts=datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(days=i),
                open=100, high=101, low=99, close=100, volume=1000, source="test",
            )
        )
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Thin Data Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    backtest_resp = await client.post(
        "/api/v1/backtests", json={"strategy_id": strategy_id, "instrument_id": str(instrument.id)}, headers=headers
    )
    job_id = backtest_resp.json()["id"]
    job = (await client.get(f"/api/v1/backtests/{job_id}", headers=headers)).json()
    assert job["status"] == "failed"
    assert "at least 30" in job["error_message"]


async def test_owner_can_delete_backtest_job(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Deletable Backtest Strategy", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    backtest_resp = await client.post(
        "/api/v1/backtests", json={"strategy_id": strategy_id, "instrument_id": str(instrument.id)}, headers=headers
    )
    job_id = backtest_resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/backtests/{job_id}", headers=headers)
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/backtests/{job_id}", headers=headers)
    assert get_resp.status_code == 404

    list_resp = await client.get(f"/api/v1/backtests?strategy_id={strategy_id}", headers=headers)
    assert job_id not in [j["id"] for j in list_resp.json()]


async def test_non_owner_cannot_delete_backtest_job(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = await _seed_instrument_with_candles(db_session)
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "TraderPassY1!"
    other = User(email="tradery1@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader Y")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    admin_token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Owned Not Deletable", "version": {"python_code": 'def generate_signal(c,p):\n    return "HOLD"'}},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    strategy_id = strategy_resp.json()["id"]
    backtest_resp = await client.post(
        "/api/v1/backtests", json={"strategy_id": strategy_id, "instrument_id": str(instrument.id)},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    job_id = backtest_resp.json()["id"]

    other_token = await _login(client, "tradery1@tradingmaster.internal", password)
    delete_resp = await client.delete(f"/api/v1/backtests/{job_id}", headers={"Authorization": f"Bearer {other_token}"})
    assert delete_resp.status_code == 403
