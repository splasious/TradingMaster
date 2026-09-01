from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.user import User
from app.services.market_data.tick_engine import tick_engine


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_reports_endpoints_reflect_a_completed_paper_trade(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    from datetime import datetime, timedelta, timezone

    instrument = Instrument(exchange="NSE", symbol="RPX", name="Report API Co", instrument_type="equity", data_source="yahoo_nse", external_ref="RPX")
    db_session.add(instrument)
    await db_session.flush()
    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 100 + i
        db_session.add(
            OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test")
        )
    await db_session.commit()
    tick_engine._last_price.pop(instrument.id, None)

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Report API Strategy", "version": {"entry_rules": {"all": [{"field": "close", "operator": ">", "value": 0}]}, "exit_rules": {"all": [{"field": "close", "operator": "<", "value": 0}]}}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    portfolio_id = (await client.get("/api/v1/paper-trading/portfolios", headers=headers)).json()[0]["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/deployments",
        json={"strategy_id": strategy_id, "instrument_id": str(instrument.id), "portfolio_id": portfolio_id, "timeframe": "1d"},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    await client.post(f"/api/v1/paper-trading/deployments/{deployment_id}/evaluate", headers=headers)

    summary_resp = await client.get("/api/v1/reports/summary", headers=headers)
    assert summary_resp.status_code == 200
    assert summary_resp.json()["trade_count"] == 0

    csv_resp = await client.get("/api/v1/reports/trades.csv", headers=headers)
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "environment,strategy,instrument,entry_ts" in csv_resp.text


async def test_reports_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/reports/summary")
    assert resp.status_code == 401
