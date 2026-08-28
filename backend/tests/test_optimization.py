from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backtest.optimization import GridTooLargeError, ParamRange, build_param_grid


def test_build_param_grid_single_axis():
    grid = build_param_grid([ParamRange(name="threshold", min=10, max=30, step=10)])
    assert grid == [{"threshold": 10}, {"threshold": 20}, {"threshold": 30}]


def test_build_param_grid_cartesian_product():
    grid = build_param_grid([ParamRange(name="a", min=1, max=2, step=1), ParamRange(name="b", min=10, max=20, step=10)])
    assert len(grid) == 4
    assert {"a": 1, "b": 10} in grid
    assert {"a": 2, "b": 20} in grid


def test_build_param_grid_rejects_zero_step():
    with pytest.raises(ValueError):
        build_param_grid([ParamRange(name="a", min=1, max=2, step=0)])


def test_build_param_grid_rejects_oversized_grid():
    with pytest.raises(GridTooLargeError):
        build_param_grid([ParamRange(name="a", min=1, max=1000, step=1)])


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


async def test_optimization_api_ranks_results(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = Instrument(exchange="NSE", symbol="OPTX", name="Opt Co", instrument_type="equity", data_source="yahoo_nse", external_ref="OPTX")
    db_session.add(instrument)
    await db_session.flush()
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(60):
        close = 100 + i * 0.5 + (2 if i % 6 == 0 else -1)
        db_session.add(
            OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test")
        )
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={
            "name": "Optimizable",
            "version": {
                "python_code": (
                    'def generate_signal(candles, params):\n'
                    '    threshold = params.get("threshold", 0)\n'
                    '    if candles[-1]["close"] - candles[0]["close"] > threshold:\n'
                    '        return "BUY"\n'
                    '    return "HOLD"\n'
                )
            },
        },
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    opt_resp = await client.post(
        "/api/v1/optimization",
        json={
            "strategy_id": strategy_id, "instrument_id": str(instrument.id), "timeframe": "1d",
            "param_ranges": [{"name": "threshold", "min": 5, "max": 15, "step": 5}], "rank_metric": "net_profit",
        },
        headers=headers,
    )
    assert opt_resp.status_code == 202
    job_id = opt_resp.json()["id"]

    job = (await client.get(f"/api/v1/optimization/{job_id}", headers=headers)).json()
    assert job["status"] == "completed", job

    result = (await client.get(f"/api/v1/optimization/{job_id}/result", headers=headers)).json()
    assert len(result["runs"]) == 3  # threshold 5, 10, 15
    ranked_profits = [r["metrics"]["net_profit"] for r in result["runs"]]
    assert ranked_profits == sorted(ranked_profits, reverse=True)


async def test_optimization_rejects_visual_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    instrument = Instrument(exchange="NSE", symbol="VISX", name="Visual Co", instrument_type="equity", data_source="yahoo_nse", external_ref="VISX")
    db_session.add(instrument)
    await db_session.commit()

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Visual Only", "version": {"entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]}, "exit_rules": {"all": []}}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    resp = await client.post(
        "/api/v1/optimization",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "param_ranges": [{"name": "x", "min": 1, "max": 2, "step": 1}]},
        headers=headers,
    )
    assert resp.status_code == 400
