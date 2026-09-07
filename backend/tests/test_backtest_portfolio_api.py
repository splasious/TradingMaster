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


async def _make_strategy(client: AsyncClient, headers: dict, name: str) -> str:
    resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": name,
            "version": {
                "entry_rules": {"all": [{"field": "rsi.rsi", "operator": ">", "value": 40}]},
                "exit_rules": {"all": [{"field": "rsi.rsi", "operator": "<", "value": 30}]},
            },
        },
        headers=headers,
    )
    return resp.json()["id"]


async def test_full_portfolio_backtest_flow_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "PFA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "PFB", 500)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_strategy(client, headers, "Portfolio Target")

    resp = await client.post(
        "/api/v1/portfolio-backtests",
        json={
            "strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)],
            "timeframe": "1d", "initial_capital": 100000, "position_size_pct": 20, "max_open_positions": 5,
        },
        headers=headers,
    )
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    assert resp.json()["status"] == "pending"

    # BackgroundTasks run synchronously within the ASGI test transport, so
    # by the time the POST above returned, the job has already executed.
    job = (await client.get(f"/api/v1/portfolio-backtests/{job_id}", headers=headers)).json()
    assert job["status"] == "completed", job

    result_resp = await client.get(f"/api/v1/portfolio-backtests/{job_id}/result", headers=headers)
    assert result_resp.status_code == 200
    result = result_resp.json()
    assert "net_profit" in result["metrics"]
    assert result["instrument_count"] == 2
    assert result["skipped_symbols"] == []
    assert len(result["equity_curve"]) == 60

    trades_resp = await client.get(f"/api/v1/portfolio-backtests/{job_id}/trades", headers=headers)
    assert trades_resp.status_code == 200
    trades = trades_resp.json()
    symbols_traded = {t["symbol"] for t in trades}
    # Both instruments got the same entry signal on their own histories --
    # a real portfolio backtest, not two isolated single-symbol runs.
    assert symbols_traded == {"PFA", "PFB"}

    strategy_after = await client.get(f"/api/v1/strategies/{strategy_id}", headers=headers)
    assert strategy_after.json()["status"] == "backtested"


async def test_portfolio_backtest_skips_instrument_with_too_few_candles(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "THICK", 100, n=60)
    thin = Instrument(exchange="NSE", symbol="THIN2", name="Thin Co", instrument_type="equity", data_source="zerodha_kite", external_ref="THIN2")
    db_session.add(thin)
    await db_session.flush()
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(5):
        db_session.add(
            OhlcvCandle(
                instrument_id=thin.id, timeframe="1d", ts=base + timedelta(days=i),
                open=100, high=101, low=99, close=100, volume=1000, source="test",
            )
        )
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_strategy(client, headers, "Partial Skip Strategy")

    resp = await client.post(
        "/api/v1/portfolio-backtests",
        json={"strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(thin.id)], "timeframe": "1d"},
        headers=headers,
    )
    job_id = resp.json()["id"]

    result = (await client.get(f"/api/v1/portfolio-backtests/{job_id}/result", headers=headers)).json()
    assert result["instrument_count"] == 1
    assert result["skipped_symbols"] == ["THIN2"]


async def test_portfolio_backtest_rejects_single_instrument(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "SOLO", 100)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_strategy(client, headers, "Solo Strategy")

    resp = await client.post(
        "/api/v1/portfolio-backtests",
        json={"strategy_id": strategy_id, "instrument_ids": [str(inst_a.id)], "timeframe": "1d"},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_non_owner_cannot_start_portfolio_backtest(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "OWNA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "OWNB", 200)
    trader_role = (await db_session.execute(select(Role).where(Role.name == "trader"))).scalar_one()
    password = "TraderPfPassX1!"
    other = User(email="traderpfx1@tradingmaster.internal", hashed_password=hash_password(password), full_name="Trader PF")
    other.user_roles = [UserRole(role=trader_role)]
    db_session.add(other)
    await db_session.commit()

    admin_token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    strategy_id = await _make_strategy(client, {"Authorization": f"Bearer {admin_token}"}, "PF Owned")

    other_token = await _login(client, "traderpfx1@tradingmaster.internal", password)
    resp = await client.post(
        "/api/v1/portfolio-backtests",
        json={"strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)]},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 403


async def test_owner_can_delete_portfolio_backtest_job(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    inst_a = await _seed_instrument_with_candles(db_session, "DELA", 100)
    inst_b = await _seed_instrument_with_candles(db_session, "DELB", 200)
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _make_strategy(client, headers, "Deletable Portfolio Strategy")

    resp = await client.post(
        "/api/v1/portfolio-backtests",
        json={"strategy_id": strategy_id, "instrument_ids": [str(inst_a.id), str(inst_b.id)]},
        headers=headers,
    )
    job_id = resp.json()["id"]

    delete_resp = await client.delete(f"/api/v1/portfolio-backtests/{job_id}", headers=headers)
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/portfolio-backtests/{job_id}", headers=headers)
    assert get_resp.status_code == 404

    list_resp = await client.get(f"/api/v1/portfolio-backtests?strategy_id={strategy_id}", headers=headers)
    assert job_id not in [j["id"] for j in list_resp.json()]
