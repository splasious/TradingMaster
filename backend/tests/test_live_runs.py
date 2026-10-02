"""The Trading page's Paper / Live switch (services/live_trading/live_runs.py,
/api/v1/live-native/runs): a live run started from a paper card on a tested
live account with a typed size, edited, resumed, turned off (close or leave)
and every live position exited -- against a simulated broker only."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.api.v1.endpoints import live_native as live_native_api
from app.api.v1.endpoints.paper_native_trading import _build_position_out
from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.live_native import (
    LIVE_NATIVE_ACTIVE,
    LIVE_NATIVE_PAUSED,
    LIVE_NATIVE_STOPPED,
    LiveAccountBaseline,
    LiveNativeDeployment,
    LiveNativePosition,
    LiveNativeTrade,
)
from app.models.paper_trading import PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.live_trading import broker_contracts, kill_switch, live_runs
from app.services.live_trading.native_gateway import Fill
from app.services.live_trading.order_state_machine import LiveOrderStatus
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.scheduler import paper_trading_scheduler

CODE = "async def evaluate(ctx):\n    ctx.note('hold', reason='nothing to do')\n"


@pytest.fixture(autouse=True)
def _fresh_contract_lists():
    broker_contracts.forget_lists()
    yield
    broker_contracts.forget_lists()


class FakeGateway:
    def __init__(self, refuse: set[str] | None = None) -> None:
        self.orders: list[tuple] = []
        self.refuse = refuse or set()

    async def key(self, instrument):
        return ("NFO", instrument.external_ref)

    async def order(self, instrument, side, quantity, product, client_order_id, price):
        self.orders.append((instrument.symbol, side, quantity))
        if instrument.symbol in self.refuse:
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, "R1", "RMS: margin")
        return Fill(LiveOrderStatus.FILLED, quantity, 95.0, f"B{len(self.orders)}", None, price)

    async def net_positions(self):
        return {}


class World:
    def __init__(self, db, client: AsyncClient, admin: dict) -> None:
        self.db, self.client, self.admin = db, client, admin

    async def build(self) -> "World":
        db = self.db
        resp = await self.client.post("/api/v1/auth/login", json={"email": self.admin["email"], "password": self.admin["password"]})
        self.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        self.user = (await db.execute(select(User).where(User.email == self.admin["email"]))).scalar_one()
        kite = (await db.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one()
        hdfc = (await db.execute(select(Broker).where(Broker.code == "hdfc_securities"))).scalar_one()
        self.account = BrokerAccount(user_id=self.user.id, broker_id=kite.id, account_label="Kite", environment="live",
                                     live_verified_at=datetime.now(timezone.utc))
        self.untested = BrokerAccount(user_id=self.user.id, broker_id=kite.id, account_label="Kite 2", environment="live")
        self.hdfc = BrokerAccount(user_id=self.user.id, broker_id=hdfc.id, account_label="H", environment="live",
                                  live_verified_at=datetime.now(timezone.utc))
        self.strategy = Strategy(name="PCR live", owner_id=self.user.id, code_type="native")
        db.add_all([self.account, self.untested, self.hdfc, self.strategy])
        await db.flush()
        self.version = StrategyVersion(strategy_id=self.strategy.id, version_number=1, timeframe="15m", instrument_ids=[],
                                       parameters={}, python_code=CODE, position_sizing={}, risk_rules={})
        self.portfolio = PaperPortfolio(user_id=self.user.id, name="P", cash=1e6, initial_capital=1e6)
        db.add_all([self.version, self.portfolio])
        await db.flush()
        self.paper = PaperNativeDeployment(portfolio_id=self.portfolio.id, strategy_id=self.strategy.id,
                                           strategy_version_id=self.version.id, status="active", state=None)
        self.option = Instrument(exchange="NFO", symbol="NIFTYT23500PE", name="x", instrument_type="option", data_source="test",
                                 external_ref="NIFTYT23500PE", strike=23500.0, option_type="PE", lot_size=65)
        db.add_all([self.paper, self.option])
        await db.commit()
        tick_engine.set_real_price(self.option.id, 100.0, source="test")
        return self

    def go_live(self, **overrides):
        body = {"paper_deployment_id": str(self.paper.id), "broker_account_id": str(self.account.id), "lots_per_leg": 2,
                "capital": 300000, "product_style": "overnight", "confirmed": True, **overrides}
        return self.client.post("/api/v1/live-native/runs", json=body, headers=self.headers)

    async def run(self) -> LiveNativeDeployment:
        return (await self.db.execute(
            select(LiveNativeDeployment).where(LiveNativeDeployment.paper_deployment_id == self.paper.id)
        )).scalar_one()

    async def hold(self, run: LiveNativeDeployment, quantity: float = -130.0) -> None:
        self.db.add(LiveNativePosition(deployment_id=run.id, instrument_id=self.option.id, quantity=quantity, avg_price=110.0,
                                       strategy_quantity=650.0, product="NRML", opened_at=datetime.now(timezone.utc)))
        await self.db.commit()


@pytest.fixture
async def world(db_session, client, seeded_admin):
    return await World(db_session, client, seeded_admin).build()


async def test_going_live_needs_confirming_a_tested_account_and_a_typed_size(world, db_session):
    async def detail(**overrides):
        return (await world.go_live(**overrides)).json()["detail"]

    assert "Confirm" in await detail(confirmed=False)
    assert "hasn't passed the broker test" in await detail(broker_account_id=str(world.untested.id))
    assert "can't trade through HDFC Securities" in await detail(broker_account_id=str(world.hdfc.id))
    assert "1 to 25" in await detail(lots_per_leg=0) and "1 to 25" in await detail(lots_per_leg=26)
    assert "Type the capital" in await detail(capital=0)
    await kill_switch.activate(db_session, world.user.id, "test")
    await db_session.commit()
    assert "kill switch is on" in await detail()
    await kill_switch.deactivate(db_session)
    await db_session.commit()

    resp = await world.go_live()
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert (out["status"], out["broker_name"], out["account_label"], out["lots_per_leg"], out["loss_limit"]) == (
        "active", "Zerodha Kite", "Kite", 2, 9000.0)
    assert out["paper_deployment_id"] == str(world.paper.id) and out["positions"] == [] and out["fix"] is None
    run = await world.run()
    assert run.state is None and run.strategy_version_id == world.version.id  # starts flat, on the card's version
    assert "already live" in await detail()
    listed = (await world.client.get("/api/v1/live-native/runs", headers=world.headers)).json()
    assert [r["id"] for r in listed] == [out["id"]]


async def test_edit_and_resume(world, db_session):
    run_id = (await world.go_live()).json()["id"]
    url = f"/api/v1/live-native/runs/{run_id}"
    edited = (await world.client.patch(url, json={"lots_per_leg": 3, "capital": 200000, "daily_loss_limit": 5000,
                                                  "product_style": "overnight"}, headers=world.headers)).json()
    assert (edited["lots_per_leg"], edited["capital"], edited["loss_limit"]) == (3, 200000, 5000)
    run = await world.run()
    await world.hold(run)
    resp = await world.client.patch(url, json={"lots_per_leg": 3, "capital": 200000, "product_style": "intraday"}, headers=world.headers)
    assert resp.status_code == 400 and "only change while it holds nothing" in resp.json()["detail"]

    assert "isn't paused" in (await world.client.post(f"{url}/resume", headers=world.headers)).json()["detail"]
    run.status, run.pause_reason = LIVE_NATIVE_PAUSED, "Couldn't log in to the broker (token expired) -- paused"
    await db_session.commit()
    [listed] = (await world.client.get("/api/v1/live-native/runs", headers=world.headers)).json()
    assert listed["fix"] == "login" and listed["positions"][0]["side"] == "short" and listed["positions"][0]["lots"] == 2
    resumed = (await world.client.post(f"{url}/resume", headers=world.headers)).json()
    assert resumed["status"] == "active" and resumed["pause_reason"] is None


async def test_leaving_positions_makes_them_yours_where_another_strategy_holds_the_contract(world, db_session, monkeypatch):
    run_id = (await world.go_live()).json()["id"]
    run = await world.run()
    await world.hold(run, -130.0)
    other = LiveNativeDeployment(owner_id=world.user.id, strategy_id=world.strategy.id, strategy_version_id=world.version.id,
                                 broker_account_id=world.account.id, status=LIVE_NATIVE_ACTIVE, lots_per_leg=1, capital=100000)
    db_session.add(other)
    await db_session.flush()
    db_session.add_all([
        LiveNativePosition(deployment_id=other.id, instrument_id=world.option.id, quantity=-65.0, avg_price=100.0,
                           strategy_quantity=65.0, product="NRML", opened_at=datetime.now(timezone.utc)),
        LiveAccountBaseline(broker_account_id=world.account.id, contract_key="NFO|NIFTYT23500PE", symbol="NIFTYT23500PE",
                            quantity=0.0, recorded_at=datetime.now(timezone.utc)),
    ])
    await db_session.commit()

    async def gateway(db, account_id):
        return FakeGateway()

    monkeypatch.setattr(live_native_api, "open_gateway", gateway)
    resp = await world.client.post(f"/api/v1/live-native/runs/{run_id}/stop", json={"close_positions": False}, headers=world.headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["left_in_account"] == ["-130 NIFTYT23500PE"]
    await db_session.refresh(run)
    baseline = (await db_session.execute(select(LiveAccountBaseline))).scalar_one()
    await db_session.refresh(baseline)
    assert run.status == LIVE_NATIVE_STOPPED and baseline.quantity == -130.0
    assert (await db_session.execute(select(LiveNativePosition).where(LiveNativePosition.deployment_id == run.id))).first() is None
    listed = (await world.client.get("/api/v1/live-native/runs", headers=world.headers)).json()
    assert [r["id"] for r in listed] == [str(other.id)]  # only the other one is still on


async def test_leaving_positions_nobody_else_holds_needs_no_broker(world, db_session, monkeypatch):
    run_id = (await world.go_live()).json()["id"]
    await world.hold(await world.run())

    async def no_broker(db, account_id):
        raise RuntimeError("not logged in")

    monkeypatch.setattr(live_native_api, "open_gateway", no_broker)
    resp = await world.client.post(f"/api/v1/live-native/runs/{run_id}/stop", json={"close_positions": False}, headers=world.headers)
    assert resp.status_code == 200 and resp.json()["left_in_account"] == ["-130 NIFTYT23500PE"]


async def test_closing_positions_when_turning_live_off(world, db_session, monkeypatch):
    run_id = (await world.go_live()).json()["id"]
    run = await world.run()
    await world.hold(run)
    url = f"/api/v1/live-native/runs/{run_id}/stop"
    monkeypatch.setattr(live_runs, "nse_market_open", lambda now: False)
    resp = await world.client.post(url, json={"close_positions": True}, headers=world.headers)
    assert resp.status_code == 409 and "NSE is closed" in resp.json()["detail"]

    monkeypatch.setattr(live_runs, "nse_market_open", lambda now: True)
    refusing = FakeGateway(refuse={"NIFTYT23500PE"})

    async def gateway(db, account_id):
        return refusing

    monkeypatch.setattr(live_native_api, "open_gateway", gateway)
    resp = await world.client.post(url, json={"close_positions": True}, headers=world.headers)
    assert resp.status_code == 409 and "Couldn't close NIFTYT23500PE" in resp.json()["detail"]
    await db_session.refresh(run)
    assert run.status == LIVE_NATIVE_PAUSED and "Turning live off" in run.pause_reason  # kept, and paused

    filling = FakeGateway()

    async def gateway2(db, account_id):
        return filling

    monkeypatch.setattr(live_native_api, "open_gateway", gateway2)
    resp = await world.client.post(url, json={"close_positions": True}, headers=world.headers)
    assert resp.status_code == 200, resp.text
    assert filling.orders == [("NIFTYT23500PE", "buy", 130.0)]
    [trade] = (await db_session.execute(select(LiveNativeTrade))).scalars().all()
    assert trade.exit_reason == "switched_off" and trade.pnl == pytest.approx((110.0 - 95.0) * 130)
    await db_session.refresh(run)
    assert run.status == LIVE_NATIVE_STOPPED


async def test_exit_all_closes_everything_and_pauses_every_live_run(world, db_session, monkeypatch):
    run_id = (await world.go_live()).json()["id"]
    run = await world.run()
    await world.hold(run)
    monkeypatch.setattr(live_runs, "nse_market_open", lambda now: False)
    resp = await world.client.post("/api/v1/live-native/exit-all", headers=world.headers)
    assert resp.status_code == 409 and "NSE is closed" in resp.json()["detail"]

    monkeypatch.setattr(live_runs, "nse_market_open", lambda now: True)
    gateway = FakeGateway()

    async def opener(db, account_id):
        return gateway

    monkeypatch.setattr(live_native_api, "open_gateway", opener)
    resp = await world.client.post("/api/v1/live-native/exit-all", headers=world.headers)
    assert resp.json() == {"paused": 1, "not_closed": []}
    assert gateway.orders == [("NIFTYT23500PE", "buy", 130.0)]
    [listed] = (await world.client.get("/api/v1/live-native/runs", headers=world.headers)).json()
    assert listed["id"] == run_id and listed["status"] == "paused" and listed["fix"] == "resume" and listed["positions"] == []

    trades = (await world.client.get("/api/v1/live-native/trades", headers=world.headers)).json()
    assert [(t["exit_reason"], t["broker_name"], t["account_label"], t["strategy_name"], t["paper_deployment_id"]) for t in trades] == [
        ("exit_all", "Zerodha Kite", "Kite", "PCR live", str(world.paper.id))]


async def test_the_paper_card_and_its_live_run_stay_together(world, db_session):
    await world.go_live()
    paper_url = f"/api/v1/paper-trading/native-deployments/{world.paper.id}"
    await world.client.post(f"{paper_url}/stop", headers=world.headers)
    resp = await world.client.delete(paper_url, headers=world.headers)
    assert resp.status_code == 409 and "turn Live off" in resp.json()["detail"]

    v2 = StrategyVersion(strategy_id=world.strategy.id, version_number=2, timeframe="15m", instrument_ids=[], parameters={},
                         python_code=CODE, position_sizing={}, risk_rules={})
    db_session.add(v2)
    await db_session.commit()
    assert (await world.client.post(f"{paper_url}/use-latest-version", headers=world.headers)).status_code == 200
    run = await world.run()
    await db_session.refresh(run)
    assert run.strategy_version_id == v2.id


async def test_another_users_run_cant_be_touched(world, db_session):
    stranger = User(email=f"s_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="S")
    db_session.add(stranger)
    await db_session.flush()
    theirs = LiveNativeDeployment(owner_id=stranger.id, strategy_id=world.strategy.id, strategy_version_id=world.version.id,
                                  broker_account_id=world.account.id, status=LIVE_NATIVE_ACTIVE, lots_per_leg=1, capital=1)
    db_session.add(theirs)
    await db_session.commit()
    for method, path, body in (("post", "resume", None), ("post", "stop", {"close_positions": False})):
        resp = await getattr(world.client, method)(f"/api/v1/live-native/runs/{theirs.id}/{path}", json=body, headers=world.headers)
        assert resp.status_code == 404


async def test_the_card_shows_its_next_check(world):
    at = datetime.now(timezone.utc) + timedelta(minutes=8)
    paper_trading_scheduler.note_wakeup(world.paper.id, at)
    try:
        [card] = (await world.client.get("/api/v1/paper-trading/native-deployments", headers=world.headers)).json()
        assert datetime.fromisoformat(card["next_check_at"]) == at
    finally:
        paper_trading_scheduler.note_wakeup(world.paper.id, None)


async def test_legs_kept_at_the_top_of_the_state_show_as_its_position(world, db_session):
    state = {"regime": "BEARISH", "pcr_at_entry": 0.71, "last_pcr": 0.6984, "last_pcr_at": "10:15", "legs": {
        "future": {"instrument_id": str(world.option.id), "side": "sell", "quantity": 650, "entry_price": 120.0,
                   "opened_at": "2026-10-05T10:15:06+05:30"},
    }}
    position = await _build_position_out(db_session, state)
    assert position.bias == "BEARISH" and len(position.legs) == 1 and position.legs[0].instrument_type == "option"
    assert position.metrics == {"total_oi_pcr_at_entry": "0.710", "total_oi_pcr_last_close": "0.698 @ 10:15"}
    assert await _build_position_out(db_session, {"regime": "FLAT", "legs": {}}) is None


def test_which_fix_a_pause_offers():
    run = LiveNativeDeployment(status=LIVE_NATIVE_PAUSED)
    cases = {
        "Couldn't log in to the broker (x) -- paused; log in again, then resume": "login",
        "This broker account hasn't passed the broker test (Settings > Brokers) -- paused until it has": "test",
        "Day P&L -9,100 reached the daily loss limit of 9,000: squared off; resumes at the next session": "loss_limit",
        "Exit all at 10:05: you closed every live position -- resume when ready": "resume",
        "Exit all at 10:05: couldn't close X -- check the broker": "check_broker",
        "Doesn't match the broker -- NIFTY: the broker holds 0, the live strategies -65.": "check_broker",
    }
    for reason, fix in cases.items():
        run.pause_reason = reason
        assert live_runs.pause_fix(run, False) == fix, reason
    assert live_runs.pause_fix(run, True) == "kill_switch"
    run.status = LIVE_NATIVE_ACTIVE
    assert live_runs.pause_fix(run, False) is None
