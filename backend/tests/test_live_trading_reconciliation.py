import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_payload
from app.models.broker import Broker, BrokerAccount, BrokerCredential
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LivePosition
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import Role, User, UserRole
from app.services.live_trading.reconciliation import reconcile_positions


def _patch_delta(monkeypatch, broker_positions: list[dict], ticker_price=150.0, product_id=27):
    async def fake_get(client_self, url, **kwargs):
        payload = {"success": True, "result": {"close": ticker_price, "mark_price": ticker_price, "product_id": product_id}}
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request("GET", str(url)))

    async def fake_request(client_self, method, url, headers=None, content=None, **kwargs):
        path = httpx.URL(str(url)).path
        if path == "/v2/wallet/balances":
            payload = {"success": True, "result": []}
        elif path == "/v2/positions":
            payload = {"success": True, "result": broker_positions}
        else:
            raise AssertionError(f"unexpected {method} {path}")
        return httpx.Response(200, content=json.dumps(payload).encode(), request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)


async def _setup(db_session: AsyncSession, local_quantity: float | None):
    role = Role(name=f"role_{uuid.uuid4().hex[:6]}", description="x")
    db_session.add(role)
    await db_session.flush()
    user = User(email=f"recon_{uuid.uuid4().hex[:8]}@tradingmaster.internal", hashed_password="x", full_name="Recon User")
    user.user_roles = [UserRole(role=role)]
    db_session.add(user)
    await db_session.flush()

    broker = Broker(code="delta_exchange", name="Delta Exchange", is_enabled=True)
    db_session.add(broker)
    await db_session.flush()
    broker_account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="recon test", environment="live")
    db_session.add(broker_account)
    await db_session.flush()
    db_session.add(BrokerCredential(broker_account_id=broker_account.id, encrypted_payload=encrypt_payload(json.dumps({"api_key": "k", "api_secret": "s"}))))

    instrument = Instrument(exchange="DELTA", symbol="RECX", name="Recon Perp", instrument_type="perpetual_future", data_source="delta_exchange", external_ref="RECX")
    db_session.add(instrument)
    await db_session.flush()

    strategy = Strategy(name="Recon Strategy", owner_id=user.id, code_type="python", status="approved")
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="1d", instrument_ids=[], parameters={}, python_code='def generate_signal(c,p):\n    return "HOLD"', position_sizing={"type": "fixed_quantity", "value": 1}, risk_rules={})
    db_session.add(version)
    await db_session.flush()

    deployment = LiveDeployment(owner_id=user.id, strategy_id=strategy.id, strategy_version_id=version.id, instrument_id=instrument.id, broker_account_id=broker_account.id, timeframe="1d", status="active")
    db_session.add(deployment)
    await db_session.flush()

    if local_quantity is not None:
        db_session.add(LivePosition(deployment_id=deployment.id, quantity=local_quantity, avg_entry_price=100.0, opened_at=datetime.now(timezone.utc)))

    await db_session.commit()
    return broker_account


async def test_reconciliation_clean_when_quantities_match(db_session: AsyncSession, monkeypatch):
    broker_account = await _setup(db_session, local_quantity=5.0)
    _patch_delta(monkeypatch, broker_positions=[{"product_id": 27, "size": 5, "product_symbol": "RECX"}])

    report = await reconcile_positions(db_session, broker_account)
    assert report.clean is True
    assert len(report.matched) == 1


async def test_reconciliation_flags_quantity_mismatch(db_session: AsyncSession, monkeypatch):
    broker_account = await _setup(db_session, local_quantity=5.0)
    _patch_delta(monkeypatch, broker_positions=[{"product_id": 27, "size": 3, "product_symbol": "RECX"}])

    report = await reconcile_positions(db_session, broker_account)
    assert report.clean is False
    assert len(report.quantity_mismatches) == 1
    assert report.quantity_mismatches[0]["local_quantity"] == 5.0
    assert report.quantity_mismatches[0]["broker_quantity"] == 3.0


async def test_reconciliation_flags_local_only_position(db_session: AsyncSession, monkeypatch):
    broker_account = await _setup(db_session, local_quantity=5.0)
    _patch_delta(monkeypatch, broker_positions=[])  # broker shows nothing

    report = await reconcile_positions(db_session, broker_account)
    assert report.clean is False
    assert len(report.local_only) == 1


async def test_reconciliation_flags_broker_only_position(db_session: AsyncSession, monkeypatch):
    broker_account = await _setup(db_session, local_quantity=None)  # no local position at all
    _patch_delta(monkeypatch, broker_positions=[{"product_id": 99, "size": 10, "product_symbol": "GHOST"}])

    report = await reconcile_positions(db_session, broker_account)
    assert report.clean is False
    assert len(report.broker_only) == 1
    assert report.broker_only[0]["product_symbol"] == "GHOST"


async def test_reconciliation_never_mutates_local_state(db_session: AsyncSession, monkeypatch):
    """Discrepancies are surfaced, never auto-corrected (PRD: never
    silently overwrite state)."""
    from sqlalchemy import select

    broker_account = await _setup(db_session, local_quantity=5.0)
    _patch_delta(monkeypatch, broker_positions=[{"product_id": 27, "size": 999, "product_symbol": "RECX"}])

    await reconcile_positions(db_session, broker_account)

    positions = (await db_session.execute(select(LivePosition))).scalars().all()
    assert len(positions) == 1
    assert positions[0].quantity == 5.0  # untouched despite the huge mismatch
