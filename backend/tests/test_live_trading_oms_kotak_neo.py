import json
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

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


class FakeKotakNeoClient:
    """Stands in for an authenticated neo_api_client.NeoAPI instance --
    routes place_order/order_report/limits/search_scrip/quotes calls to
    canned responses shaped exactly like the real SDK's (confirmed by
    reading its source on GitHub, see kotak_neo_broker.py's module
    docstring), so OMS tests never touch the real SDK or network."""

    def __init__(self, ltp=1500.0, available_margin=100000.0, order_status="complete"):
        self.ltp = ltp
        self.available_margin = available_margin
        self.order_status = order_status
        self.placed_orders: list[dict] = []

    def totp_login(self, mobile_number=None, ucc=None, totp=None):
        return {"data": {"token": "view_tok"}}

    def totp_validate(self, mpin=None):
        return {"data": {"token": "trade_tok"}}

    def search_scrip(self, exchange_segment=None, symbol=None, expiry=None, option_type=None, strike_price=None):
        return {"data": [{"pSymbol": "12345"}]}

    def quotes(self, instrument_tokens=None, quote_type=None):
        return {"data": [{"last_traded_price": self.ltp}]}

    def limits(self, segment=None, exchange=None, product=None):
        return {"data": {"Net": str(self.available_margin)}}

    def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        return {"data": {"nOrdNo": "KN998877"}}

    def order_report(self):
        return {"data": [{"nOrdNo": "KN998877", "ordSt": self.order_status}]}

    def positions(self):
        return {"data": []}


@pytest.fixture
def fake_kotak_sdk(monkeypatch):
    client_holder: dict[str, FakeKotakNeoClient] = {}

    def _neo_api_factory(environment=None, access_token=None, neo_fin_key=None, consumer_key=None):
        client = client_holder.setdefault("client", FakeKotakNeoClient())
        return client

    fake_neo_module = types.SimpleNamespace(NeoAPI=_neo_api_factory)
    fake_pyotp_module = types.SimpleNamespace(TOTP=lambda secret: types.SimpleNamespace(now=lambda: "123456"))
    monkeypatch.setitem(sys.modules, "neo_api_client", fake_neo_module)
    monkeypatch.setitem(sys.modules, "pyotp", fake_pyotp_module)
    return client_holder


async def _setup(db_session: AsyncSession, *, entry_rules=None, exit_rules=None):
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"kotak_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Kotak User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code="kotak_neo", name="Kotak Neo", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="kotak live test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(broker_account_id=broker_account.id, status=ConnectionStatus.CONNECTED.value))
    db_session.add(BrokerCredential(
        broker_account_id=broker_account.id,
        encrypted_payload=encrypt_payload(json.dumps({
            "consumer_key": "ck", "mobile_number": "+919999999999", "ucc": "ABC123",
            "totp_secret": "JBSWY3DPEHPK3PXP", "mpin": "123456",
        })),
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

    strategy = Strategy(name="Kotak Live Strategy", owner_id=user.id, code_type="visual", status=StrategyStatus.APPROVED.value)
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


async def test_entry_places_real_kotak_neo_order(db_session: AsyncSession, fake_kotak_sdk):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "entered"
    assert outcome.signal == "BUY"

    client = fake_kotak_sdk["client"]
    assert len(client.placed_orders) == 1
    order = client.placed_orders[0]
    assert order["transaction_type"] == "B"
    assert order["trading_symbol"] == "INFY"
    assert order["exchange_segment"] == "NSE"
    assert order["product"] == "CNC"
    assert order["order_type"] == "MKT"
    assert order["quantity"] == "2"

    live_order = (await db_session.execute(select(LiveOrder).where(LiveOrder.deployment_id == ctx["deployment"].id))).scalar_one()
    assert live_order.status == "filled"  # KOTAK_NEO_STATE_MAP["complete"] -> FILLED
    assert live_order.broker_order_id == "KN998877"

    position = (await db_session.execute(select(LivePosition).where(LivePosition.deployment_id == ctx["deployment"].id))).scalar_one()
    assert position.quantity == 2
    assert position.avg_entry_price == 1500.0


async def test_exit_signal_closes_kotak_neo_position(db_session: AsyncSession, fake_kotak_sdk):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    await evaluate_live_deployment(db_session, ctx["deployment"])  # enters

    version = await db_session.get(StrategyVersion, ctx["deployment"].strategy_version_id)
    version.entry_rules = NEVER
    version.exit_rules = ALWAYS_BUY
    await db_session.commit()

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "exited"
    position = (await db_session.execute(select(LivePosition).where(LivePosition.deployment_id == ctx["deployment"].id))).scalar_one_or_none()
    assert position is None

    client = fake_kotak_sdk["client"]
    assert len(client.placed_orders) == 2
    assert client.placed_orders[1]["transaction_type"] == "S"
