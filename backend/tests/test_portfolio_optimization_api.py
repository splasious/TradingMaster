from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _seed_instrument_with_candles(db_session: AsyncSession, symbol: str, base_price: float, n=60) -> Instrument:
    instrument = Instrument(
        exchange="NSE", symbol=symbol, name=f"{symbol} Co", instrument_type="equity",
        data_source="zerodha_kite", external_ref=symbol,
    )
    db_session.add(instrument)
    await db_session.flush()
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(n):
        close = base_price + i * 0.8 + (3 if i % 7 == 0 else 0) - (2 if i % 5 == 0 else 0)
        db_session.add(
            OhlcvCandle(
                instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5,
                high=close + 1, low=close - 1.5, close=close, volume=1000.0, source="test",
            )
        )
    await db_session.commit()
    return instrument


async def _make_python_strategy(client: AsyncClient, headers: dict, name: str, code: str) -> str:
    resp = await client.post(
        "/api/v1/strategies",
        json={"name": name, "version": {"python_code": code}},
        headers=headers,
    )
    return resp.json()["id"]


THRESHOLD_CODE = (
    'def generate_signal(candles, params):\n'
    '    threshold = params.get("threshold", 0)\n'
    '    if candles[-1]["close"] - candles[0]["close"] > threshold:\n'
    '        return "BUY"\n'
    '    return "HOLD"\n'
)


async def test_portfolio_optimization_ranks_combinations_across_the_basket(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "POA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "POB", 500)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_python_strategy(client, headers, "Portfolio Optimizable", THRESHOLD_CODE)

    resp = await client.post(
        "/api/v1/portfolio-optimization",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)], "timeframe": "1d",
            "param_ranges": [{"name": "threshold", "min": 5, "max": 15, "step": 5}], "rank_metric": "net_profit",
        },
        headers=headers,
    )
    assert resp.status_code == 202
    job_id = resp.json()["id"]

    job = (await client.get(f"/api/v1/portfolio-optimization/{job_id}", headers=headers)).json()
    assert job["status"] == "completed", job

    result = (await client.get(f"/api/v1/portfolio-optimization/{job_id}/result", headers=headers)).json()
    assert len(result["runs"]) == 3  # threshold 5, 10, 15
    ranked_profits = [r["metrics"]["net_profit"] for r in result["runs"]]
    assert ranked_profits == sorted(ranked_profits, reverse=True)
    for run in result["runs"]:
        assert run["instrument_count"] == 2
        assert run["skipped_symbols"] == []

    strategy_after = (await client.get(f"/api/v1/strategies/{strategy_id}", headers=headers)).json()
    assert strategy_after["status"] == "backtested"


async def test_portfolio_optimization_rejects_visual_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "POVA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "POVB", 200)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Visual Only Portfolio", "version": {"entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]}, "exit_rules": {"all": []}}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post(
        "/api/v1/portfolio-optimization",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)],
            "param_ranges": [{"name": "x", "min": 1, "max": 2, "step": 1}],
        },
        headers=headers,
    )
    assert resp.status_code == 400


async def test_portfolio_optimization_rejects_single_instrument(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "POSOLO", 100)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_python_strategy(client, headers, "Solo Portfolio Opt", THRESHOLD_CODE)

    resp = await client.post(
        "/api/v1/portfolio-optimization",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id)],
            "param_ranges": [{"name": "threshold", "min": 1, "max": 2, "step": 1}],
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_portfolio_optimization_skips_instrument_with_too_few_candles(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "POTHICK", 100, n=60)
    thin = Instrument(exchange="NSE", symbol="POTHIN", name="Thin Co", instrument_type="equity", data_source="zerodha_kite", external_ref="POTHIN")
    db_session.add(thin)
    await db_session.flush()
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(5):
        db_session.add(
            OhlcvCandle(instrument_id=thin.id, timeframe="1d", ts=base + timedelta(days=i), open=100, high=101, low=99, close=100, volume=1000, source="test")
        )
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_python_strategy(client, headers, "Partial Skip Opt", THRESHOLD_CODE)

    resp = await client.post(
        "/api/v1/portfolio-optimization",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(thin.id)], "timeframe": "1d",
            "param_ranges": [{"name": "threshold", "min": 5, "max": 5, "step": 1}],
        },
        headers=headers,
    )
    job_id = resp.json()["id"]
    result = (await client.get(f"/api/v1/portfolio-optimization/{job_id}/result", headers=headers)).json()
    assert result["runs"][0]["instrument_count"] == 1


async def test_portfolio_optimization_rejects_grid_too_large(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "POBIGA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "POBIGB", 200)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_python_strategy(client, headers, "Too Big Grid", THRESHOLD_CODE)

    resp = await client.post(
        "/api/v1/portfolio-optimization",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)],
            "param_ranges": [{"name": "threshold", "min": 1, "max": 1000, "step": 1}],
        },
        headers=headers,
    )
    job_id = resp.json()["id"]
    job = (await client.get(f"/api/v1/portfolio-optimization/{job_id}", headers=headers)).json()
    assert job["status"] == "failed"
    assert "exceeding the cap" in job["error_message"]


async def test_non_owner_cannot_start_portfolio_optimization(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from sqlalchemy import select

    from app.core.security import hash_password
    from app.models.user import Role, User, UserRole

    inst_a = await _seed_instrument_with_candles(db_session, "PONOA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "PONOB", 200)
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "TraderPoPassX1!"
    other = User(email="traderpox1@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader PO")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    admin_token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    strategy_id = await _make_python_strategy(client, {"Authorization": f"Bearer {admin_token}"}, "PO Owned", THRESHOLD_CODE)

    other_token = await _login(client, "traderpox1@tradingmaster.internal", password)
    resp = await client.post(
        "/api/v1/portfolio-optimization",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)],
            "param_ranges": [{"name": "threshold", "min": 1, "max": 2, "step": 1}],
        },
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


async def test_owner_can_delete_portfolio_optimization_job(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "PODELA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "PODELB", 200)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_python_strategy(client, headers, "Deletable Portfolio Opt", THRESHOLD_CODE)

    resp = await client.post(
        "/api/v1/portfolio-optimization",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)],
            "param_ranges": [{"name": "threshold", "min": 1, "max": 2, "step": 1}],
        },
        headers=headers,
    )
    job_id = resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/portfolio-optimization/{job_id}", headers=headers)
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/portfolio-optimization/{job_id}", headers=headers)
    assert get_resp.status_code == 404

    list_resp = await client.get(f"/api/v1/portfolio-optimization?strategy_id={strategy_id}", headers=headers)
    assert job_id not in [j["id"] for j in list_resp.json()]
