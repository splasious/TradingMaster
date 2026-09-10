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


class FakeHDFCTransport:
    """Routes HDFCSecuritiesBroker's httpx.AsyncClient.request calls to
    canned responses keyed by path -- this adapter's own module docstring
    is explicit that HDFC's exact field names are inferred, not confirmed
    (their docs are a JS app that couldn't be rendered); this test proves
    the wiring through oms.py works for whatever shape they do turn out
    to use, not that the shape itself is correct."""

    def __init__(self, ltp=1500.0, order_status="COMPLETE"):
        self.ltp = ltp
        self.order_status = order_status
        self.placed_orders: list[dict] = []

    def _respond(self, method: str, path: str, params, json_body):
        if path == "/profile":
            return 200, {"client_id": "C123"}
        if path == "/quote":
            return 200, {"last_price": self.ltp}
        if path == "/funds":
            return 200, {"available_margin": 100000.0, "used_margin": 0.0}
        if path == "/positions":
            return 200, {"positions": []}
        if path == "/orders" and method == "POST":
            self.placed_orders.append(json_body)
            return 200, {"order_id": "HDFC998877"}
        if path == "/orders/HDFC998877" and method == "GET":
            return 200, {"status": self.order_status}
        return 200, {"data": []}

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_request(client_self, method, url, headers=None, params=None, json=None, **kwargs):
            path = httpx.URL(url).path.removeprefix("/oapi/v1")
            status, payload = self._respond(method, path, params, json)
            return httpx.Response(status, json=payload, request=httpx.Request(method, str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def _setup(db_session: AsyncSession, *, entry_rules=None, exit_rules=None):
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"hdfc_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="HDFC User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code="hdfc_securities", name="HDFC Securities", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="hdfc live test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(broker_account_id=broker_account.id, status=ConnectionStatus.CONNECTED.value))
    db_session.add(BrokerCredential(
        broker_account_id=broker_account.id,
        encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s", "access_token": "t"})),
    ))

    instrument = Instrument(
        exchange="NSE", symbol="INFY", name="Infosys Ltd", instrument_type="equity",
        data_source="zerodha_kite", external_ref="INFY",
    )
    db_session.add(instrument)
    await db_session.flush()

    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 1000 + i
        db_session.add(OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test"))

    strategy = Strategy(name="HDFC Live Strategy", owner_id=user.id, code_type="visual", status=StrategyStatus.APPROVED.value)
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[str(instrument.id)], parameters={},
        entry_rules=entry_rules, exit_rules=exit_rules, python_code=None,
        position_sizing={"type": "fixed_quantity", "value": 2}, risk_rules={"stop_loss_pct": 5.0},
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


async def test_entry_places_real_hdfc_order(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeHDFCTransport(ltp=1500.0)
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

    live_order = (await db_session.execute(select(LiveOrder).where(LiveOrder.deployment_id == ctx["deployment"].id))).scalar_one()
    assert live_order.status == "filled"  # HDFC_STATE_MAP["complete"] -> FILLED (lowercased before lookup? see below)
    assert live_order.broker_order_id == "HDFC998877"

    position = (await db_session.execute(select(LivePosition).where(LivePosition.deployment_id == ctx["deployment"].id))).scalar_one()
    assert position.quantity == 2
    assert position.avg_entry_price == 1500.0
