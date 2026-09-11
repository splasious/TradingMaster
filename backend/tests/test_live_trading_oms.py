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


def _ticker_response(price: float, product_id: int = 27) -> dict:
    return {"success": True, "result": {"close": price, "mark_price": price, "product_id": product_id}}


class FakeDeltaTransport:
    """Routes both DeltaExchangeDataSource (httpx.AsyncClient.get) and
    DeltaExchangeBroker (httpx.AsyncClient.request) calls to canned
    responses keyed by path, so OMS tests never touch the real network."""

    def __init__(self, ticker_price=150.0, balance=100000.0, place_order_state="open", order_status_state="closed"):
        self.ticker_price = ticker_price
        self.balance = balance
        self.place_order_state = place_order_state
        self.order_status_state = order_status_state
        self.placed_orders: list[dict] = []

    def _respond(self, method: str, url: str, content: bytes | None):
        path = httpx.URL(url).path
        if path.startswith("/v2/tickers/"):
            return 200, _ticker_response(self.ticker_price)
        if path == "/v2/wallet/balances":
            return 200, {"success": True, "result": [{"asset_symbol": "USD", "balance": str(self.balance), "available_balance": str(self.balance)}]}
        if path == "/v2/orders" and method == "POST":
            body = json.loads(content)
            self.placed_orders.append(body)
            return 200, {"success": True, "result": {"id": 555, "state": self.place_order_state, "unfilled_size": 0}}
        if path.startswith("/v2/orders/") and method == "GET":
            return 200, {"success": True, "result": {"id": 555, "state": self.order_status_state}}
        if path == "/v2/positions":
            return 200, {"success": True, "result": []}
        raise AssertionError(f"Unexpected request: {method} {path}")

    def patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_get(client_self, url, **kwargs):
            status, payload = self._respond("GET", str(url), None)
            return httpx.Response(status, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

        async def fake_request(client_self, method, url, headers=None, content=None, **kwargs):
            status, payload = self._respond(method, str(url), content)
            return httpx.Response(status, content=json.dumps(payload).encode(), request=httpx.Request(method, str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def _setup(db_session: AsyncSession, *, entry_rules=None, exit_rules=None, python_code=None, risk_rules=None, timeframe: str = "1d"):
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"live_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Live User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="live test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(broker_account_id=broker_account.id, status=ConnectionStatus.CONNECTED.value))
    db_session.add(BrokerCredential(broker_account_id=broker_account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s"}))))

    instrument = Instrument(exchange="DELTA", symbol="LIVX", name="Live Test Perp", instrument_type="perpetual_future", data_source="delta_exchange", external_ref="LIVX")
    db_session.add(instrument)
    await db_session.flush()

    base = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(30):
        close = 100 + i
        db_session.add(OhlcvCandle(instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i), open=close - 0.5, high=close + 1, low=close - 1, close=close, volume=1000, source="test"))

    strategy = Strategy(name="Live Strategy", owner_id=user.id, code_type="python" if python_code else "visual", status=StrategyStatus.APPROVED.value)
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version_number=1, timeframe=timeframe, instrument_ids=[str(instrument.id)], parameters={},
        entry_rules=entry_rules, exit_rules=exit_rules, python_code=python_code,
        position_sizing={"type": "fixed_quantity", "value": 2}, risk_rules=risk_rules or {"stop_loss_pct": 5.0},
    )
    db_session.add(version)
    await db_session.flush()

    deployment = LiveDeployment(
        owner_id=user.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id,
        broker_account_id=broker_account.id, timeframe=timeframe, status="active",
    )
    db_session.add(deployment)
    await db_session.commit()

    return {"user": user, "broker_account": broker_account, "instrument": instrument, "deployment": deployment}


ALWAYS_BUY = {"all": [{"field": "close", "operator": ">", "value": 0}]}
NEVER = {"all": [{"field": "close", "operator": "<", "value": 0}]}


async def test_entry_places_real_order_and_confirms_status(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeDeltaTransport(ticker_price=150.0, place_order_state="open", order_status_state="closed")
    fake.patch(monkeypatch)

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "entered"
    assert outcome.signal == "BUY"

    assert len(fake.placed_orders) == 1
    assert fake.placed_orders[0]["side"] == "buy"
    assert fake.placed_orders[0]["product_id"] == 27

    order = (await db_session.execute(select(LiveOrder).where(LiveOrder.deployment_id == ctx["deployment"].id))).scalar_one()
    assert order.status == "filled"  # confirmed via get_order_status ("closed" -> FILLED), not just the placement response
    assert order.broker_order_id == "555"
    assert order.confirmed_at is not None

    position = (await db_session.execute(select(LivePosition).where(LivePosition.deployment_id == ctx["deployment"].id))).scalar_one()
    assert position.quantity == 2
    assert position.avg_entry_price == 150.0

    await db_session.refresh(ctx["deployment"])
    assert ctx["deployment"].last_signal == "BUY"  # the outcome's signal, not the "entered" action word


async def test_kill_switch_blocks_evaluation_entirely(db_session: AsyncSession, monkeypatch):
    from app.services.live_trading.kill_switch import activate

    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    await activate(db_session, ctx["user"].id, "emergency test")
    await db_session.commit()

    fake = FakeDeltaTransport()
    fake.patch(monkeypatch)

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "blocked"
    assert not fake.placed_orders  # no order was ever attempted


async def test_insufficient_balance_rejects_without_placing_order(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeDeltaTransport(ticker_price=150.0, balance=1.0)  # can't afford even 1 unit
    fake.patch(monkeypatch)

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "rejected"
    assert "Insufficient cash" in outcome.reason
    assert not fake.placed_orders


async def test_exit_signal_closes_position(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeDeltaTransport(ticker_price=150.0)
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
    assert fake.placed_orders[1]["side"] == "sell"


async def test_stop_loss_exits_before_checking_signal(db_session: AsyncSession, monkeypatch):
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER, risk_rules={"stop_loss_pct": 5.0})
    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)
    await evaluate_live_deployment(db_session, ctx["deployment"])  # enters at 150

    fake.ticker_price = 140.0  # breaches 5% stop (142.5)
    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "exited"
    assert outcome.reason == "stop_loss"


async def test_daily_loss_limit_blocks_new_entry_after_a_real_loss(db_session: AsyncSession, monkeypatch):
    """Regression test: _try_enter used to pass realized_pnl_today=0.0
    (hardcoded), so max_daily_loss_pct could never actually trigger in
    live trading no matter how much was lost. This drives a real losing
    round-trip through evaluate_live_deployment (not a direct risk-engine
    unit test) so it fails the same way the original bug did if the fix
    regresses -- balance small enough (1000) that a 5% limit (50) is
    breached by a real 100-unit loss on a 2-quantity, 50-point drop."""
    ctx = await _setup(
        db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER, risk_rules={"max_daily_loss_pct": 5.0},
    )
    fake = FakeDeltaTransport(ticker_price=150.0, balance=1000.0)
    fake.patch(monkeypatch)
    await evaluate_live_deployment(db_session, ctx["deployment"])  # enters at 150, quantity 2

    version = await db_session.get(StrategyVersion, ctx["deployment"].strategy_version_id)
    version.entry_rules = NEVER
    version.exit_rules = ALWAYS_BUY
    await db_session.commit()
    fake.ticker_price = 100.0  # exits at 100 -> pnl = (100-150)*2 = -100, breaching the 50 limit
    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "exited"

    version.entry_rules = ALWAYS_BUY
    version.exit_rules = NEVER
    await db_session.commit()
    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "rejected"
    assert "Daily loss limit breached" in outcome.reason
    assert len(fake.placed_orders) == 2  # the entry + exit above -- no third (re-entry) order placed


async def test_1wk_deployment_derives_signal_from_stored_daily_candles(db_session: AsyncSession, monkeypatch):
    """Kite/Delta have no native weekly interval for every source -- a
    "1wk" live deployment must not just silently see zero candles forever.
    load_candles' resample fallback derives real weekly bars from the
    "1d" candles _setup seeds."""
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER, timeframe="1wk")
    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "entered"
    assert outcome.signal == "BUY"


async def test_stale_candle_data_blocks_trading_and_raises_critical_alert(db_session: AsyncSession, monkeypatch):
    """check_freshness itself is unit-tested directly in
    test_market_data_freshness.py -- this only confirms
    evaluate_live_deployment actually calls it and refuses to place a real
    order on stale data (CRITICAL severity, unlike paper trading's
    WARNING -- real money is what's at risk). Mocked at the
    check_freshness boundary rather than depending on real wall-clock NSE
    market hours."""
    ctx = await _setup(db_session, entry_rules=ALWAYS_BUY, exit_rules=NEVER)
    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)

    import app.services.live_trading.oms as oms_module
    monkeypatch.setattr(oms_module, "check_freshness", lambda candles, timeframe, now: "latest candle is way too old")

    outcome = await evaluate_live_deployment(db_session, ctx["deployment"])
    assert outcome.action == "blocked"
    assert outcome.reason == "latest candle is way too old"
    assert len(fake.placed_orders) == 0  # never reached order placement

    from app.models.alert import Alert, AlertSeverity, AlertType
    alerts = (await db_session.execute(select(Alert).where(Alert.alert_type == AlertType.DATA_DISCONNECTED.value))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.CRITICAL.value

    await db_session.refresh(ctx["deployment"])
    assert ctx["deployment"].last_evaluated_at is not None  # a blocked tick still counts as "the scheduler reached this deployment"
    assert ctx["deployment"].last_signal == "BLOCKED"
    assert ctx["deployment"].last_signal_reason == "latest candle is way too old"
