import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.core.security import hash_password
from app.models.broker import Broker, BrokerAccount, BrokerConnection, BrokerCredential, ConnectionStatus
from app.models.instrument import Instrument
from app.models.live_trading import LiveOrder
from app.models.user import Role, User, UserRole
from app.services.live_trading.manual_orders import ManualOrderError, place_manual_order


def _ticker_response(price: float, product_id: int = 27) -> dict:
    return {"success": True, "result": {"close": price, "mark_price": price, "product_id": product_id}}


class FakeDeltaTransport:
    """Same fake used by test_live_trading_oms.py -- routes Delta's ticker/
    balance/order calls to canned responses so these tests never touch the
    real network. Delta is used here (rather than Kite) because the safety
    pipeline under test is broker-agnostic; which adapter answers the calls
    doesn't matter to what's being verified."""

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
        # httpx.AsyncClient.request/.get is patched at the class level, which
        # also intercepts the `client` fixture's own ASGI-transport calls to
        # the FastAPI app in tests that use both -- only absolute (real-host)
        # URLs are Delta calls; relative ASGI-transport URLs fall through to
        # the real, unpatched implementation.
        original_get = httpx.AsyncClient.get
        original_request = httpx.AsyncClient.request

        async def fake_get(client_self, url, **kwargs):
            if not httpx.URL(str(url)).host:
                return await original_get(client_self, url, **kwargs)
            status, payload = self._respond("GET", str(url), None)
            return httpx.Response(status, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

        async def fake_request(client_self, method, url, headers=None, content=None, **kwargs):
            if not httpx.URL(str(url)).host:
                return await original_request(client_self, method, url, headers=headers, content=content, **kwargs)
            status, payload = self._respond(method, str(url), content)
            return httpx.Response(status, content=json.dumps(payload).encode(), request=httpx.Request(method, str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def _setup_manual(
    db_session: AsyncSession, *, environment: str = "live", connection_status: str = ConnectionStatus.CONNECTED.value,
    instrument_type: str = "perpetual_future", lot_size: int | None = None,
) -> dict:
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"manual_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Manual Order User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="manual test", environment=environment)
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(broker_account_id=broker_account.id, status=connection_status))
    db_session.add(BrokerCredential(broker_account_id=broker_account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s"}))))

    instrument = Instrument(
        exchange="DELTA", symbol="MANX", name="Manual Test Instrument", instrument_type=instrument_type,
        data_source="delta_exchange", external_ref="MANX", lot_size=lot_size,
    )
    db_session.add(instrument)
    await db_session.commit()

    return {"user": user, "broker_account": broker_account, "instrument": instrument}


async def _other_trader(db_session: AsyncSession) -> User:
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    other = User(
        email=f"othertrader_{uuid.uuid4().hex[:8]}@tradingmaster.internal",
        hashed_password=hash_password("OtherTraderX1!"), full_name="Other Trader",
    )
    other.user_roles = [UserRole(role=role)]
    db_session.add(other)
    await db_session.commit()
    return other


async def test_manual_order_happy_path(db_session: AsyncSession, monkeypatch):
    ctx = await _setup_manual(db_session)
    fake = FakeDeltaTransport(ticker_price=150.0, order_status_state="closed")
    fake.patch(monkeypatch)

    live_order = await place_manual_order(
        db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
        side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
    )

    assert live_order.deployment_id is None
    assert live_order.instrument_id == ctx["instrument"].id
    assert live_order.broker_account_id == ctx["broker_account"].id
    assert live_order.owner_id == ctx["user"].id
    assert live_order.status == "filled"  # confirmed via get_order_status ("closed" -> FILLED), not just the placement response
    assert live_order.broker_order_id == "555"
    assert live_order.confirmed_at is not None
    assert len(fake.placed_orders) == 1


async def test_manual_order_requires_confirmed_flag(db_session: AsyncSession, monkeypatch):
    ctx = await _setup_manual(db_session)
    fake = FakeDeltaTransport()
    fake.patch(monkeypatch)

    with pytest.raises(ManualOrderError, match="confirmation"):
        await place_manual_order(
            db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=False,
        )
    assert not fake.placed_orders
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_blocked_by_kill_switch(db_session: AsyncSession, monkeypatch):
    from app.services.live_trading.kill_switch import activate

    ctx = await _setup_manual(db_session)
    await activate(db_session, ctx["user"].id, "emergency test")
    await db_session.commit()
    fake = FakeDeltaTransport()
    fake.patch(monkeypatch)

    with pytest.raises(ManualOrderError, match="Kill switch"):
        await place_manual_order(
            db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
        )
    assert not fake.placed_orders
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_rejects_non_owner(db_session: AsyncSession, monkeypatch):
    ctx = await _setup_manual(db_session)
    other = await _other_trader(db_session)
    fake = FakeDeltaTransport()
    fake.patch(monkeypatch)

    with pytest.raises(ManualOrderError, match="Not your broker account"):
        await place_manual_order(
            db_session, other, instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
        )
    assert not fake.placed_orders
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_rejects_paper_environment_account(db_session: AsyncSession, monkeypatch):
    ctx = await _setup_manual(db_session, environment="paper")
    fake = FakeDeltaTransport()
    fake.patch(monkeypatch)

    with pytest.raises(ManualOrderError, match="'live' environment"):
        await place_manual_order(
            db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
        )
    assert not fake.placed_orders
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_rejects_disconnected_broker(db_session: AsyncSession, monkeypatch):
    ctx = await _setup_manual(db_session, connection_status=ConnectionStatus.DISCONNECTED.value)
    fake = FakeDeltaTransport()
    fake.patch(monkeypatch)

    with pytest.raises(ManualOrderError, match="not connected"):
        await place_manual_order(
            db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
        )
    assert not fake.placed_orders
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_rejects_non_lot_size_multiple(db_session: AsyncSession, monkeypatch):
    ctx = await _setup_manual(db_session, instrument_type="future", lot_size=50)
    fake = FakeDeltaTransport()
    fake.patch(monkeypatch)

    with pytest.raises(ManualOrderError, match="multiple of the lot size"):
        await place_manual_order(
            db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=25, order_type="market", limit_price=None, product="NRML", confirmed=True,
        )
    assert not fake.placed_orders  # lot-size check happens before any broker call
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_rejects_over_notional_cap(db_session: AsyncSession, monkeypatch):
    import app.services.live_trading.manual_orders as manual_orders_module

    ctx = await _setup_manual(db_session)
    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)

    class _TinyCapSettings:
        manual_order_max_notional = 10.0

    monkeypatch.setattr(manual_orders_module, "get_settings", lambda: _TinyCapSettings())

    with pytest.raises(ManualOrderError, match="exceeds the manual-order cap"):
        await place_manual_order(
            db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
        )
    assert not fake.placed_orders
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_rejects_over_available_margin(db_session: AsyncSession, monkeypatch):
    ctx = await _setup_manual(db_session)
    fake = FakeDeltaTransport(ticker_price=150.0, balance=1.0)  # notional 300 far exceeds this
    fake.patch(monkeypatch)

    with pytest.raises(ManualOrderError, match="Insufficient margin"):
        await place_manual_order(
            db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
            side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
        )
    assert not fake.placed_orders
    assert (await db_session.execute(select(LiveOrder))).scalars().all() == []


async def test_manual_order_endpoint_end_to_end(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession, monkeypatch):
    admin_login = await client.post("/api/v1/auth/login", json=seeded_admin)
    admin_row = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()

    # seeded_admin already seeds a "delta_exchange" Broker catalog row -- reuse it rather than
    # inserting a duplicate (Broker.code is unique).
    broker = (await db_session.execute(select(Broker).where(Broker.code == "delta_exchange"))).scalar_one()
    broker_account = BrokerAccount(user_id=admin_row.id, broker_id=broker.id, account_label="admin manual", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerConnection(broker_account_id=broker_account.id, status=ConnectionStatus.CONNECTED.value))
    db_session.add(BrokerCredential(broker_account_id=broker_account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s"}))))
    instrument = Instrument(exchange="DELTA", symbol="MANAPI", name="Manual API Instrument", instrument_type="perpetual_future", data_source="delta_exchange", external_ref="MANAPI")
    db_session.add(instrument)
    await db_session.commit()

    fake = FakeDeltaTransport(ticker_price=200.0)
    fake.patch(monkeypatch)

    token = admin_login.json()["access_token"]
    resp = await client.post(
        "/api/v1/live-trading/orders/manual",
        json={
            "instrument_id": str(instrument.id), "broker_account_id": str(broker_account.id), "side": "buy",
            "quantity": 1, "order_type": "market", "product": "CNC", "confirmed": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["instrument_symbol"] == "MANAPI"
    assert body["status"] == "filled"

    orders_resp = await client.get("/api/v1/live-trading/orders", headers={"Authorization": f"Bearer {token}"})
    assert orders_resp.status_code == 200
    manual_rows = [o for o in orders_resp.json() if o["deployment_id"] is None]
    assert any(o["instrument_symbol"] == "MANAPI" and o["strategy_name"] == "Manual order" for o in manual_rows)


async def test_manual_order_not_visible_to_other_non_admin_user(db_session: AsyncSession, client: AsyncClient, monkeypatch):
    ctx = await _setup_manual(db_session)
    fake = FakeDeltaTransport(ticker_price=150.0)
    fake.patch(monkeypatch)

    await place_manual_order(
        db_session, ctx["user"], instrument_id=str(ctx["instrument"].id), broker_account_id=str(ctx["broker_account"].id),
        side="buy", quantity=2, order_type="market", limit_price=None, product="CNC", confirmed=True,
    )

    other = await _other_trader(db_session)
    other_password = "OtherTraderX1!"
    login = await client.post("/api/v1/auth/login", json={"email": other.email, "password": other_password})
    token = login.json()["access_token"]

    resp = await client.get("/api/v1/live-trading/orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert all(o["instrument_symbol"] != "MANX" for o in resp.json())
