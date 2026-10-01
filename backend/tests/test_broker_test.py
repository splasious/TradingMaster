"""The broker test (services/live_trading/broker_test.py, POST
/api/v1/live-native/broker-test): a one-share round trip through the same
gateway live strategies use, every step checked, against a simulated
broker -- no real broker anywhere."""

import uuid
from datetime import date, datetime, timezone

from httpx import AsyncClient
from sqlalchemy import select

from app.api.v1.endpoints import live_native as live_native_api
from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.live_trading import LiveOrder
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.live_trading import broker_test
from app.services.live_trading.broker_contracts import BrokerContract, ContractNotFound
from app.services.live_trading.native_gateway import Fill
from app.services.live_trading.order_state_machine import LiveOrderStatus

NOW = datetime(2026, 10, 5, 11, 0, tzinfo=IST).astimezone(timezone.utc)


class TestGateway:
    __test__ = False

    def __init__(self) -> None:
        self.held = {("NSE", "IDEA"): 5.0}
        self.orders: list[tuple] = []
        self.refuse_sell = False
        self.unknown: set[str] = set()
        self.lag = 0  # position reads that still show the old quantity

    async def contract(self, instrument):
        if instrument.symbol in self.unknown:
            raise ContractNotFound(f"{instrument.symbol}: not in the broker's list")
        return BrokerContract(("NSE", instrument.symbol), instrument.symbol, "NSE", "1", instrument.lot_size)

    async def key(self, instrument):
        return (await self.contract(instrument)).key

    async def order(self, instrument, side, quantity, product, client_order_id, price):
        self.orders.append((instrument.symbol, side, quantity, product))
        if side == "sell" and self.refuse_sell:
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, "S1", "RMS: blocked", 9.9)
        self.held[("NSE", instrument.symbol)] += quantity if side == "buy" else -quantity
        self.lag = 1
        return Fill(LiveOrderStatus.FILLED, quantity, price, f"O{len(self.orders)}", None, price * 1.01)

    async def net_positions(self):
        if self.lag:
            self.lag -= 1
            moved = dict(self.held)
            last = self.orders[-1]
            moved[("NSE", last[0])] -= 1 if last[1] == "buy" else -1
            return moved
        return dict(self.held)


async def _world(db):
    user = User(email=f"bt_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="T")
    db.add(user)
    broker = (await db.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one_or_none()
    if broker is None:
        broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
        db.add(broker)
    await db.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Kite", environment="live")
    stock = Instrument(exchange="NSE", symbol="IDEA", name="IDEA", instrument_type="equity", data_source="zerodha_kite",
                       external_ref="IDEA")
    options = [
        Instrument(exchange="NFO", symbol=f"NIFTY26106{strike}{kind}", name="NIFTY", instrument_type="option", data_source="z",
                   external_ref=f"NIFTY26106{strike}{kind}", strike=float(strike), option_type=kind, lot_size=65, expiry=date(2026, 10, 6))
        for strike in (22600, 22700, 22800) for kind in ("CE", "PE")
    ]
    future = Instrument(exchange="NFO", symbol="NIFTY26OCTFUT", name="NIFTY", instrument_type="future", data_source="z",
                        external_ref="NIFTY26OCTFUT", lot_size=65, expiry=date(2026, 10, 27))
    db.add_all([account, stock, future, *options])
    await db.commit()
    return user, account, stock


async def _price(db, instrument, now):
    return 9.8


async def test_a_clean_round_trip_marks_the_account_tested(db_session, monkeypatch):
    monkeypatch.setattr(broker_test, "live_price", _price)
    monkeypatch.setattr(broker_test, "POSITION_CHECK_SECONDS", 0)
    user, account, stock = await _world(db_session)
    gateway = TestGateway()
    report = await broker_test.run_broker_test(db_session, account, user.id, stock, gateway, NOW)

    assert report.passed and account.live_verified_at == NOW
    assert [s.name for s in report.steps] == ["Contracts", "Holdings", "Buy", "Position", "Sell", "Position"]
    assert [c["ours"] for c in report.contracts] == ["IDEA", "NIFTY2610622700CE", "NIFTY2610622700PE", "NIFTY26OCTFUT"]
    assert gateway.orders == [("IDEA", "buy", 1, "MIS"), ("IDEA", "sell", 1, "MIS")] and gateway.held[("NSE", "IDEA")] == 5.0
    orders = (await db_session.execute(select(LiveOrder).where(LiveOrder.purpose == "broker_test"))).scalars().all()
    assert sorted(o.side for o in orders) == ["buy", "sell"] and all(o.native_deployment_id is None for o in orders)


async def test_a_sell_that_fails_leaves_it_untested_and_says_a_share_is_held(db_session, monkeypatch):
    monkeypatch.setattr(broker_test, "live_price", _price)
    monkeypatch.setattr(broker_test, "POSITION_CHECK_SECONDS", 0)
    user, account, stock = await _world(db_session)
    gateway = TestGateway()
    gateway.refuse_sell = True
    report = await broker_test.run_broker_test(db_session, account, user.id, stock, gateway, NOW)
    assert not report.passed and account.live_verified_at is None
    assert report.steps[-1].name == "Sell" and "RMS: blocked" in report.steps[-1].detail
    assert "1 share of IDEA is still held intraday" in report.still_held


async def test_a_contract_it_cant_match_stops_before_any_order(db_session, monkeypatch):
    monkeypatch.setattr(broker_test, "live_price", _price)
    user, account, stock = await _world(db_session)
    gateway = TestGateway()
    gateway.unknown = {"NIFTY26OCTFUT"}
    report = await broker_test.run_broker_test(db_session, account, user.id, stock, gateway, NOW)
    assert not report.passed and gateway.orders == [] and report.steps[0].detail == "3 of 4 matched"
    assert report.contracts[-1] == {"ours": "NIFTY26OCTFUT", "error": "NIFTY26OCTFUT: not in the broker's list", "ok": False}


def test_it_runs_only_while_the_market_is_open_and_not_near_the_close():
    assert broker_test.can_test_now(NOW) is None
    assert "closed" in broker_test.can_test_now(datetime(2026, 10, 4, 11, 0, tzinfo=IST))  # Sunday
    assert "before 15:00" in broker_test.can_test_now(datetime(2026, 10, 5, 15, 5, tzinfo=IST))


async def _headers(client: AsyncClient, admin: dict) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": admin["email"], "password": admin["password"]})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_the_endpoint_refuses_what_it_cant_test(client: AsyncClient, seeded_admin: dict, db_session, monkeypatch):
    headers = await _headers(client, seeded_admin)
    admin = (await db_session.execute(select(User).where(User.email == seeded_admin["email"]))).scalar_one()
    brokers = {}
    for code, name in (("zerodha_kite", "Zerodha Kite"), ("kotak_neo", "Kotak Neo")):
        brokers[code] = (await db_session.execute(select(Broker).where(Broker.code == code))).scalar_one_or_none()
        if brokers[code] is None:
            brokers[code] = Broker(code=code, name=name, is_enabled=True)
            db_session.add(brokers[code])
    await db_session.flush()
    paper = BrokerAccount(user_id=admin.id, broker_id=brokers["zerodha_kite"].id, account_label="P", environment="paper")
    kotak = BrokerAccount(user_id=admin.id, broker_id=brokers["kotak_neo"].id, account_label="K", environment="live")
    kite = BrokerAccount(user_id=admin.id, broker_id=brokers["zerodha_kite"].id, account_label="L", environment="live")
    db_session.add_all([paper, kotak, kite])
    await db_session.commit()

    async def post(account, symbol="IDEA"):
        return await client.post("/api/v1/live-native/broker-test", json={"broker_account_id": str(account.id), "symbol": symbol},
                                 headers=headers)

    assert "Only a live broker account" in (await post(paper)).json()["detail"]
    assert "can't trade through Kotak Neo yet" in (await post(kotak)).json()["detail"]
    monkeypatch.setattr(live_native_api, "can_test_now", lambda now: "NSE is closed -- run the test while the market is open")
    assert (await post(kite)).json()["detail"].startswith("NSE is closed")
    monkeypatch.setattr(live_native_api, "can_test_now", lambda now: None)
    resp = await post(kite, "NOSUCH")
    assert resp.status_code == 400 and "NOSUCH isn't in this app's NSE stock list" in resp.json()["detail"]

    accounts = (await client.get("/api/v1/brokers/accounts", headers=headers)).json()
    listed = next(a for a in accounts if a["id"] == str(kite.id))
    assert listed["live_verified_at"] is None and listed["broker"]["supports_live_strategies"] is True


async def test_server_ip_reports_what_the_server_goes_out_as(client: AsyncClient, seeded_admin: dict, monkeypatch):
    answers = {"https://api.ipify.org": "203.0.113.7", "https://api64.ipify.org": "2001:db8::7"}

    async def ask(url):
        return answers[url]

    monkeypatch.setattr(live_native_api, "_ask", ask)
    monkeypatch.setattr(live_native_api, "_ip_cache", None)
    headers = await _headers(client, seeded_admin)
    assert (await client.get("/api/v1/live-native/server-ip", headers=headers)).json() == {"ipv4": "203.0.113.7", "ipv6": "2001:db8::7"}
