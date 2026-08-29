import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LiveOrder, LivePosition
from app.models.market_data import OhlcvCandle
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.live_trading.oms import evaluate_live_deployment
from app.services.strategy.state_machine import StrategyStatus


class FakeKiteTransport:
    """Routes ZerodhaKiteBroker's httpx.AsyncClient.request calls to canned
    Kite Connect v3 responses keyed by path, so OMS tests never touch the
    real network -- no Kite developer subscription is available."""

    def __init__(self, ltp=1500.0, available_margin=100000.0, order_status="COMPLETE"):
        self.ltp = ltp
        self.available_margin = available_margin
        self.order_status = order_status
        self.placed_orders: list[dict] = []

    def _respond(self, method: str, url: str, params, data):
        path = httpx.URL(url).path
        if path == "/user/profile":
            return 200, {"status": "success", "data": {"user_id": "AB1234"}}
        if path == "/user/margins":
            return 200, {"status": "success", "data": {"equity": {"available": {"live_balance": self.available_margin}, "utilised": {"debits": 0}}}}
        if path == "/quote/ltp":
            instrument = params["i"]
            return 200, {"status": "success", "data": {instrument: {"last_price": self.ltp, "instrument_token": 408065}}}
        if path == "/orders/regular" and method == "POST":
            self.placed_orders.append(data)
            return 200, {"status": "success", "data": {"order_id": "230925000012345"}}
        if path == "/orders/230925000012345" and method == "GET":
            return 200, {"status": "success", "data": [{"status": self.order_status}]}
        if path == "/portfolio/positions":
            return 200, {"status": "success", "data": {"net": []}}
        raise AssertionError(f"Unexpected request: {method} {path}")

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_request(client_self, method, url, headers=None, params=None, data=None, **kwargs):
            status, payload = self._respond(method, str(url), params, data)
            return httpx.Response(status, json=payload, request=httpx.Request(method, str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def _setup(db_session: AsyncSession, *, entry_rules=None, exit_rules=None, risk_rules=None):
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"kite_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Kite User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="kite live test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(broker_account_id=broker_account.id, status=ConnectionStatus.CONNECTED.value))
    db_session.add(BrokerCredential(broker_account_id=broker_account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s", "access_token": "t"}))))

    instrument = Instrument(exchange="NSE", symbol="INFY", name="Infosys Ltd", instrument_type="equity", data_source="yahoo_nse", external_ref="INFY")
    db_session.add(instrument)
    await db_session.flush()

    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 1000 + i
        db_session.add(OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test"))

    strategy = Strategy(name="Kite Live Strategy", owner_id=user.id, code_type="visual", status=StrategyStatus.APPROVED.value)
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[str(instrument.id)], parameters={},
        entry_rules=entry_rules, exit_rules=exit_rules, python_code=None,
        position_sizing={"type": "fixed_quantity", "value": 2}, risk_rules=risk_rules or {"stop_loss_pct": 5.0},
    )
    db_session.add(version)
    await db_session.flush()

    deployment = LiveDeployment(
        owner_id=user.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        broker_account_id=broker_account.id, timeframe="1d", status="active",
    )
    db_session.add(deployment)
    await db_session.commit()

    return {"user": user, "broker_account": broker_account, "instrument": instrument, "deployment": deployment}


ALWAYS_BUY = {"all": [{"field": "close", "operator": ">", "value": 0}]}
NEVER = {"all": [{"field": "close", "operator": "<", "value": 0}]}


async def test_entry_places_real_kite_order_with_tradingsymbol_context(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeKiteTransport(ltp=1500.0)
    fake.patch(monkeypatch)

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "entered"
    assert outcome.signal == "BUY"

    assert len(fake.placed_orders) == 1
    order = fake.placed_orders[0]
    assert order["transaction_type"] == "BUY"
    assert order["tradingsymbol"] == "INFY"
    assert order["exchange"] == "NSE"
    assert order["product"] == "CNC"
    assert order["order_type"] == "MARKET"

    live_order = (await db_session.execute(select(LiveOrder).where(LiveOrder.deployment_id == ctx["deployment"].id))).scalar_one()
    assert live_order.status == "filled"  # KITE_STATE_MAP["COMPLETE"] -> FILLED
    assert live_order.broker_order_id == "230925000012345"

    position = (await db_session.execute(select(LivePosition).where(LivePosition.deployment_id == ctx["deployment"].id))).scalar_one()
    assert position.quantity == 2
    assert position.avg_entry_price == 1500.0


async def test_exit_signal_closes_kite_position(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeKiteTransport(ltp=1500.0)
    fake.patch(monkeypatch)
    await evaluate_live_deployment(db_session, ctx["deployment"])  # enters

    version = await db_session.get(StrategyVersion, ctx["deployment"].strategy_version_id)
    version.entry_rules = NEVER
    version.exit_rules = ALWAYS_BUY
    await db_session.commit()

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "exited"
    position = (await db_session.execute(select(LivePosition).where(LivePosition.deployment_id == ctx["deployment"].id))).scalar_one_or_none()
    assert position is None
    assert len(fake.placed_orders) == 2
    assert fake.placed_orders[1]["transaction_type"] == "SELL"


async def test_expired_kite_session_surfaces_as_authentication_error(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)

    async def fake_request(client_self, method, url, headers=None, params=None, data=None, **kwargs):
        return httpx.Response(403, json={"status": "error", "error_type": "TokenException", "message": "Session expired"}, request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "error"
    assert "Session expired" in outcome.reason
