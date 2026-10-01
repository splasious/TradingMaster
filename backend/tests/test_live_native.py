"""Native strategies trading live (services/live_trading/native_live.py,
native_gateway.py, native_scheduler.py) against a simulated broker: legs
become real-sized market orders, buys first; a failed leg closes what the
batch opened and puts the strategy back; the limits, the kill switch and
reconciliation pause or stop it. No real broker is involved anywhere."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.alert import Alert
from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.live_native import (
    LIVE_NATIVE_ACTIVE,
    LIVE_NATIVE_PAUSED,
    PRODUCT_INTRADAY,
    LiveAccountBaseline,
    LiveNativeDeployment,
    LiveNativePosition,
    LiveNativeTrade,
    LiveRiskSettings,
)
from app.models.live_trading import LiveOrder
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.services.broker.zerodha_broker import IST
from app.services.live_trading import broker_contracts, kill_switch, native_gateway, native_live
from app.services.live_trading.native_gateway import BrokerGateway, Fill
from app.services.live_trading.native_scheduler import LiveNativeScheduler
from app.services.live_trading.order_state_machine import LiveOrderStatus
from app.services.market_data.tick_engine import tick_engine

NOW = datetime(2026, 10, 5, 10, 0, tzinfo=IST).astimezone(timezone.utc)  # Monday, market open

SPREAD = '''
from sqlalchemy import select
from app.models.instrument import Instrument

async def evaluate(ctx):
    short = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == "NIFTYT22700CE"))).scalar_one()
    hedge = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == "NIFTYT22900CE"))).scalar_one()
    if ctx.state.pop("force_exit", False) or ctx.state.get("want") == "close":
        if ctx.state.get("open"):
            await ctx.close_leg(short, "buy", 650, 90.0)
            await ctx.close_leg(hedge, "sell", 650, 40.0)
            await ctx.record_trade(legs=[{"instrument_id": str(short.id)}, {"instrument_id": str(hedge.id)}],
                                   pnl=0.0, pnl_pct=0.0, exit_reason="pcr_flip", opened_at=ctx.now)
            ctx.state["open"] = False
            ctx.note("exited", signal="COVER", reason="spread closed")
        return
    if ctx.state.get("want") == "open" and not ctx.state.get("open"):
        await ctx.open_leg(short, "sell", 650, 100.0)
        await ctx.open_leg(hedge, "buy", 650, 50.0)
        ctx.state["open"] = True
        ctx.note("entered", signal="SPREAD", reason="spread opened")
        return
    ctx.note("hold", reason="nothing to do")
'''

STOCK = '''
from sqlalchemy import select
from app.models.instrument import Instrument

async def evaluate(ctx):
    stock = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == "SBIN"))).scalar_one()
    if ctx.state.get("want") == "buy" and not ctx.state.get("held"):
        await ctx.open_leg(stock, "buy", ctx.state.get("qty", 100), 800.0)
        ctx.state["held"] = True
        ctx.note("entered", signal="BUY", reason="bought")
'''


@pytest.fixture(autouse=True)
def _fresh_contract_lists():
    broker_contracts.forget_lists()
    yield
    broker_contracts.forget_lists()


class FakeGateway:
    """A broker that fills at set prices unless told to refuse; it holds
    what it filled, so reconciliation can compare."""

    def __init__(self) -> None:
        self.orders: list[tuple] = []
        self.prices: dict[str, float] = {"NIFTYT22700CE": 102.0, "NIFTYT22900CE": 51.0, "SBIN": 801.0}
        self.refuse: dict[tuple[str, str], str] = {}
        self.partial: dict[tuple[str, str], float] = {}
        self.held: dict[tuple[str, str], float] = {}
        self.sent_prices: list[float] = []

    def _key(self, instrument):
        return ("NFO" if instrument.instrument_type in ("option", "future") else "NSE", instrument.external_ref)

    async def key(self, instrument):
        return self._key(instrument)

    async def order(self, instrument, side, quantity, product, client_order_id, price):
        self.orders.append((instrument.symbol, side, quantity, product))
        self.sent_prices.append(price)
        reason = self.refuse.get((instrument.symbol, side))
        if reason:
            return Fill(LiveOrderStatus.REJECTED, 0.0, None, f"R{len(self.orders)}", reason)
        filled = self.partial.get((instrument.symbol, side), quantity)
        key = self._key(instrument)
        self.held[key] = self.held.get(key, 0.0) + (filled if side == "buy" else -filled)
        status = LiveOrderStatus.FILLED if filled >= quantity else LiveOrderStatus.CANCELLED
        return Fill(status, filled, self.prices[instrument.symbol], f"B{len(self.orders)}", None if filled >= quantity else "partly filled")

    async def net_positions(self):
        return dict(self.held)


async def _setup(db, code: str = SPREAD, **overrides) -> tuple[LiveNativeDeployment, dict]:
    user = User(email=f"live_{uuid.uuid4().hex[:6]}@tradingmaster.internal", hashed_password="x", full_name="Live")
    db.add(user)
    broker = (await db.execute(select(Broker).where(Broker.code == "zerodha_kite"))).scalar_one_or_none()
    if broker is None:
        broker = Broker(code="zerodha_kite", name="Zerodha Kite", is_enabled=True)
        db.add(broker)
    await db.flush()
    account = BrokerAccount(user_id=user.id, broker_id=broker.id, account_label="Kite", environment="live", live_verified_at=NOW)
    strategy = Strategy(name="Live test", owner_id=user.id, code_type="native")
    db.add_all([account, strategy])
    await db.flush()
    version = StrategyVersion(strategy_id=strategy.id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
                              python_code=code, position_sizing={}, risk_rules={})
    db.add(version)
    instruments = {
        "short": Instrument(exchange="NFO", symbol="NIFTYT22700CE", name="x", instrument_type="option", data_source="test",
                            external_ref="NIFTYT22700CE", strike=22700.0, option_type="CE", lot_size=65),
        "hedge": Instrument(exchange="NFO", symbol="NIFTYT22900CE", name="x", instrument_type="option", data_source="test",
                            external_ref="NIFTYT22900CE", strike=22900.0, option_type="CE", lot_size=65),
        "stock": Instrument(exchange="NSE", symbol="SBIN", name="SBIN", instrument_type="equity", data_source="test", external_ref="SBIN"),
    }
    db.add_all(instruments.values())
    await db.flush()
    fields = {"lots_per_leg": 2, "capital": 500000.0, "state": {"want": "open"}} | overrides
    deployment = LiveNativeDeployment(owner_id=user.id, strategy_id=strategy.id, strategy_version_id=version.id,
                                      broker_account_id=account.id, status=LIVE_NATIVE_ACTIVE, **fields)
    db.add(deployment)
    await db.commit()
    return deployment, instruments


async def _positions(db, deployment):
    rows = (await db.execute(select(LiveNativePosition).where(LiveNativePosition.deployment_id == deployment.id))).scalars().all()
    return {r.instrument_id: r for r in rows}


async def _orders(db, deployment):
    return (await db.execute(
        select(LiveOrder).where(LiveOrder.native_deployment_id == deployment.id).order_by(LiveOrder.created_at, LiveOrder.client_order_id)
    )).scalars().all()


async def test_legs_become_real_sized_market_orders_buys_first(db_session):
    deployment, inst = await _setup(db_session)
    gateway = FakeGateway()
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)

    # The code sells first; the hedge (a buy) goes first. 2 lots x 65, not the code's 650.
    assert gateway.orders == [("NIFTYT22900CE", "buy", 130.0, "NRML"), ("NIFTYT22700CE", "sell", 130.0, "NRML")]
    held = await _positions(db_session, deployment)
    assert (held[inst["short"].id].quantity, held[inst["short"].id].avg_price, held[inst["short"].id].strategy_quantity) == (-130.0, 102.0, 650.0)
    assert (held[inst["hedge"].id].quantity, held[inst["hedge"].id].avg_price) == (130.0, 51.0)
    assert deployment.state["open"] is True and out.signal == "SPREAD" and deployment.last_signal == "SPREAD"
    orders = await _orders(db_session, deployment)
    assert {(o.purpose, o.status, o.filled_quantity, o.average_price) for o in orders} == {
        ("open", "filled", 130.0, 51.0), ("open", "filled", 130.0, 102.0)}


async def test_intraday_strategies_use_mis(db_session):
    deployment, _ = await _setup(db_session, product_style=PRODUCT_INTRADAY)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert {o[3] for o in gateway.orders} == {"MIS"}


async def test_a_rejected_leg_closes_the_filled_one_and_puts_the_strategy_back(db_session):
    deployment, inst = await _setup(db_session)
    gateway = FakeGateway()
    gateway.refuse[("NIFTYT22700CE", "sell")] = "insufficient margin"
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)

    assert gateway.orders == [("NIFTYT22900CE", "buy", 130.0, "NRML"), ("NIFTYT22700CE", "sell", 130.0, "NRML"),
                              ("NIFTYT22900CE", "sell", 130.0, "NRML")]  # the hedge closed again
    assert await _positions(db_session, deployment) == {} and gateway.held[("NFO", "NIFTYT22900CE")] == 0
    assert deployment.state == {"want": "open"} and deployment.status == LIVE_NATIVE_ACTIVE  # back to before, still running
    assert out.signal == "LEG_FAILED" and "insufficient margin" in out.reason
    assert [o.purpose for o in await _orders(db_session, deployment)].count("rollback") == 1
    [trade] = (await db_session.execute(select(LiveNativeTrade))).scalars().all()
    assert trade.exit_reason == "leg_failed_rollback" and trade.pnl == 0.0  # in and out at the same fill
    assert (await db_session.execute(select(Alert).where(Alert.title == "Live entry not completed"))).scalar_one()


async def test_a_failed_rollback_pauses_it(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()
    gateway.refuse[("NIFTYT22700CE", "sell")] = "insufficient margin"
    gateway.refuse[("NIFTYT22900CE", "sell")] = "broker down"
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert deployment.status == LIVE_NATIVE_PAUSED and out.signal == "ROLLBACK_FAILED"
    assert "open at the broker now" in deployment.pause_reason
    held = await _positions(db_session, deployment)
    assert [p.quantity for p in held.values()] == [130.0]  # the hedge it couldn't close is what it really holds


async def test_closing_records_the_trade_at_real_fills(db_session):
    deployment, inst = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    deployment.state = {**deployment.state, "want": "close"}
    gateway.prices.update({"NIFTYT22700CE": 80.0, "NIFTYT22900CE": 30.0})
    await native_live.run_live_native(db_session, deployment, gateway, NOW + timedelta(minutes=15))

    assert gateway.orders[2:] == [("NIFTYT22700CE", "buy", 130.0, "NRML"), ("NIFTYT22900CE", "sell", 130.0, "NRML")]
    assert await _positions(db_session, deployment) == {}
    [trade] = (await db_session.execute(select(LiveNativeTrade))).scalars().all()
    # short 102 -> 80 (+22), long 51 -> 30 (-21), 130 units each
    assert trade.exit_reason == "pcr_flip" and trade.pnl == pytest.approx(130 * 22 - 130 * 21)
    assert {leg["side"] for leg in trade.legs} == {"short", "long"}


async def test_an_exit_leg_failing_after_the_other_closed_pauses_it(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    deployment.state = {**deployment.state, "want": "close"}
    gateway.refuse[("NIFTYT22900CE", "sell")] = "exchange closed"
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW + timedelta(minutes=15))
    assert out.signal == "PARTIAL_EXIT" and deployment.status == LIVE_NATIVE_PAUSED
    assert deployment.state["open"] is True  # the strategy's view is put back; the broker's real one is in the positions
    assert [p.quantity for p in (await _positions(db_session, deployment)).values()] == [130.0]


async def test_a_partial_fill_counts_as_failed_and_its_filled_part_is_closed(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()
    gateway.partial[("NIFTYT22700CE", "sell")] = 65.0  # one lot of two
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert ("NIFTYT22700CE", "buy", 65.0, "NRML") in gateway.orders  # the filled lot bought back
    assert ("NIFTYT22900CE", "sell", 130.0, "NRML") in gateway.orders  # and the hedge closed
    assert await _positions(db_session, deployment) == {} and deployment.status == LIVE_NATIVE_ACTIVE


async def test_the_order_limit_pauses_before_anything_is_sent(db_session):
    deployment, _ = await _setup(db_session, max_orders_per_day=1)
    gateway = FakeGateway()
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert gateway.orders == [] and out.signal == "ORDER_LIMIT" and deployment.status == LIVE_NATIVE_PAUSED


async def test_the_capital_cap_refuses_a_buy_it_cannot_pay_for(db_session):
    deployment, _ = await _setup(db_session, STOCK, capital=100000.0, state={"want": "buy", "qty": 1000})
    gateway = FakeGateway()
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert gateway.orders == [] and out.signal == "CAPITAL_CAP" and deployment.status == LIVE_NATIVE_ACTIVE
    assert deployment.state == {"want": "buy", "qty": 1000}  # nothing recorded as bought

    deployment.state = {"want": "buy", "qty": 100}  # 80,000: inside the cap
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert gateway.orders == [("SBIN", "buy", 100.0, "CNC")]


async def test_the_kill_switch_stops_everything(db_session):
    deployment, _ = await _setup(db_session)
    await kill_switch.activate(db_session, deployment.owner_id, "test")
    gateway = FakeGateway()
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert gateway.orders == [] and out.signal == "KILL_SWITCH"


def _price(instrument, price):
    tick_engine.set_real_price(instrument.id, price, source="test")
    tick_engine._real_price_at[instrument.id] = NOW


async def test_the_daily_loss_limit_squares_off_and_pauses_until_the_next_session(db_session):
    deployment, inst = await _setup(db_session, daily_loss_limit=1000.0)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    _price(inst["short"], 120.0)  # short 102 -> 120: -18 x 130 = -2,340
    _price(inst["hedge"], 51.0)
    try:
        out = await native_live.run_live_native(db_session, deployment, gateway, NOW + timedelta(minutes=1))
    finally:
        tick_engine.forget([inst["short"].id, inst["hedge"].id])
    assert out.signal == "LOSS_LIMIT" and deployment.status == LIVE_NATIVE_PAUSED
    assert gateway.orders[2:] == [("NIFTYT22700CE", "buy", 130.0, "NRML"), ("NIFTYT22900CE", "sell", 130.0, "NRML")]
    assert await _positions(db_session, deployment) == {} and deployment.state == {}
    assert deployment.resume_at is not None and deployment.resume_at.replace(tzinfo=timezone.utc) > NOW + timedelta(hours=12)
    assert {t.exit_reason for t in (await db_session.execute(select(LiveNativeTrade))).scalars().all()} == {"daily_loss_limit"}


async def test_the_account_limit_squares_everything_off_and_turns_the_kill_switch_on(db_session):
    deployment, inst = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    db_session.add(LiveRiskSettings(user_id=deployment.owner_id, account_daily_loss_limit=500.0))
    await db_session.commit()
    _price(inst["short"], 110.0)  # -8 x 130 = -1,040
    _price(inst["hedge"], 51.0)

    async def gateway_for(_):
        return gateway

    try:
        hit = await native_live.check_account_limits(db_session, gateway_for, NOW + timedelta(minutes=1))
    finally:
        tick_engine.forget([inst["short"].id, inst["hedge"].id])
    assert hit == [deployment.owner_id] and deployment.status == LIVE_NATIVE_PAUSED
    assert (await kill_switch.get_kill_switch(db_session)).active is True
    assert await _positions(db_session, deployment) == {}


async def test_reconciliation_pauses_on_a_mismatch_only(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert await native_live.reconcile_account(db_session, deployment.broker_account_id, gateway, NOW) == []
    assert deployment.status == LIVE_NATIVE_ACTIVE

    gateway.held[("NFO", "NIFTYT22700CE")] = 0.0  # closed in the broker's own app
    differences = await native_live.reconcile_account(db_session, deployment.broker_account_id, gateway, NOW)
    assert differences == ["NIFTYT22700CE: the broker holds 0, the live strategies -130"]
    assert deployment.status == LIVE_NATIVE_PAUSED and "Doesn't match the broker" in deployment.pause_reason


async def test_closing_something_not_held_live_pauses_it(db_session):
    deployment, _ = await _setup(db_session, state={"want": "close", "open": True})
    gateway = FakeGateway()
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert gateway.orders == [] and out.signal == "ORDER_PROBLEM" and deployment.status == LIVE_NATIVE_PAUSED


async def test_stop_and_exit_runs_the_strategys_own_exit(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    await native_live.run_live_native(db_session, deployment, gateway, NOW + timedelta(minutes=1), force_exit=True)
    assert await _positions(db_session, deployment) == {} and deployment.state["open"] is False


async def test_the_scheduler_runs_only_while_the_market_is_open_and_lifts_due_pauses(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()

    async def factory(db, account_id):
        return gateway

    scheduler = LiveNativeScheduler(gateway_factory=factory)
    sunday = datetime(2026, 10, 4, 10, 0, tzinfo=IST).astimezone(timezone.utc)
    assert await scheduler.tick(db_session, sunday) == 0 and gateway.orders == []

    assert await scheduler.tick(db_session, NOW) == 1 and len(gateway.orders) == 2

    deployment.status, deployment.resume_at = LIVE_NATIVE_PAUSED, NOW
    await db_session.commit()
    await scheduler.tick(db_session, NOW + timedelta(minutes=2))
    assert deployment.status == LIVE_NATIVE_ACTIVE and deployment.resume_at is None


class _KiteLike:
    """A broker adapter speaking Kite's order vocabulary, with a two-row
    instrument dump."""

    def __init__(self, statuses):
        self.placed, self.cancelled, self.statuses = [], [], list(statuses)

    async def place_order(self, order):
        self.placed.append(order)
        return {"broker_order_id": "K1"}

    async def get_order_status(self, order_id):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    async def cancel_order(self, order_id, context=None):
        self.cancelled.append(order_id)

    async def get_positions(self):
        return [{"exchange": "NFO", "tradingsymbol": "NIFTYT22700CE", "quantity": -130}]

    async def get_holdings(self):
        return [{"tradingsymbol": "SBIN", "quantity": 90, "t1_quantity": 10}]

    async def get_instruments(self, segment):
        if segment == "NSE":
            return [{"tradingsymbol": "SBIN", "name": "STATE BANK OF INDIA", "expiry": "", "strike": "0", "lot_size": "1",
                     "instrument_type": "EQ", "exchange": "NSE", "exchange_token": "3045"}]
        return [{"tradingsymbol": "NIFTY2610622700CE", "name": "NIFTY", "expiry": "2026-10-06", "strike": "22700.0", "lot_size": "65",
                 "instrument_type": "CE", "exchange": "NFO", "exchange_token": "40001"}]


async def _kite_option(db):
    option = Instrument(exchange="NFO", symbol="NIFTY2610622700CE", name="NIFTY", instrument_type="option", data_source="test",
                        external_ref="NIFTY2610622700CE", strike=22700.0, option_type="CE", lot_size=65, expiry=date(2026, 10, 6))
    db.add(option)
    await db.flush()
    return option


async def test_the_gateway_sends_a_protected_limit_in_kites_words_and_waits_for_the_fill(db_session, monkeypatch):
    monkeypatch.setattr(native_gateway, "POLL_SECONDS", 0)
    option = await _kite_option(db_session)
    broker = _KiteLike([{"status": "OPEN", "raw": {}}, {"status": "COMPLETE", "raw": {"filled_quantity": 130, "average_price": 101.5}}])
    fill = await BrokerGateway("zerodha_kite", broker).order(option, "sell", 130.0, "NRML", "tmn-x", 102.0)
    assert broker.placed == [{"tradingsymbol": "NIFTY2610622700CE", "exchange": "NFO", "token": "40001", "product": "NRML",
                              "quantity": 130.0, "side": "sell", "order_type": "limit", "limit_price": 100.95, "client_order_id": "tmn-x"}]
    assert (fill.status, fill.filled_quantity, fill.average_price, fill.limit_price) == (LiveOrderStatus.FILLED, 130.0, 101.5, 100.95)

    rejected = await BrokerGateway("zerodha_kite", _KiteLike([{"status": "REJECTED", "raw": {"status_message": "RMS: margin"}}])).order(
        option, "sell", 130.0, "NRML", "tmn-y", 102.0)
    assert rejected.status == LiveOrderStatus.REJECTED and rejected.reason == "RMS: margin"

    gateway = BrokerGateway("zerodha_kite", broker)
    assert await gateway.net_positions() == {("NFO", "NIFTYT22700CE"): -130.0, ("NSE", "SBIN"): 100.0}


async def test_a_contract_the_broker_doesnt_list_is_never_sent(db_session):
    option = await _kite_option(db_session)
    option.lot_size = 75  # the broker says 65
    broker = _KiteLike([{"status": "COMPLETE", "raw": {}}])
    fill = await BrokerGateway("zerodha_kite", broker).order(option, "buy", 75.0, "NRML", "tmn-l", 100.0)
    assert broker.placed == [] and fill.status == LiveOrderStatus.REJECTED and "lot size 65 at the broker" in fill.reason
    fill = await BrokerGateway("hdfc_securities", broker).order(option, "buy", 75.0, "NRML", "tmn-h", 100.0)
    assert broker.placed == [] and "can't trade through hdfc_securities" in fill.reason


async def test_an_order_still_open_after_the_timeout_is_cancelled(db_session, monkeypatch):
    monkeypatch.setattr(native_gateway, "POLL_SECONDS", 0)
    monkeypatch.setattr(native_gateway, "FILL_TIMEOUT_SECONDS", 0)
    option = await _kite_option(db_session)
    broker = _KiteLike([{"status": "OPEN", "raw": {"filled_quantity": 65}}])
    fill = await BrokerGateway("zerodha_kite", broker).order(option, "sell", 130.0, "NRML", "tmn-z", 102.0)
    assert broker.cancelled == ["K1"] and fill.status == LiveOrderStatus.CANCELLED and fill.filled_quantity == 65.0


async def test_a_second_failed_entry_in_a_day_pauses_it(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()
    gateway.refuse[("NIFTYT22700CE", "sell")] = "insufficient margin"
    first = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert first.action == "rolled_back" and deployment.status == LIVE_NATIVE_ACTIVE  # once: flat, keeps running
    second = await native_live.run_live_native(db_session, deployment, gateway, NOW + timedelta(seconds=10))
    assert second.action == "paused" and deployment.status == LIVE_NATIVE_PAUSED
    assert "failed 2 times today" in deployment.pause_reason
    assert await _positions(db_session, deployment) == {} and len(gateway.orders) == 6  # two tries, each rolled back
    # the next day starts afresh
    deployment.status = LIVE_NATIVE_ACTIVE
    await db_session.commit()
    third = await native_live.run_live_native(db_session, deployment, gateway, NOW + timedelta(days=1))
    assert third.action == "rolled_back" and deployment.status == LIVE_NATIVE_ACTIVE


async def test_a_failed_exit_keeps_the_position_and_says_so(db_session):
    deployment, _ = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    deployment.state = {**deployment.state, "want": "close"}
    gateway.refuse[("NIFTYT22700CE", "buy")] = "circuit limit"  # the first exit leg, so nothing closes
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW + timedelta(minutes=15))
    assert out.signal == "LEG_FAILED" and deployment.status == LIVE_NATIVE_ACTIVE and deployment.state["open"] is True
    assert sorted(p.quantity for p in (await _positions(db_session, deployment)).values()) == [-130.0, 130.0]
    assert (await db_session.execute(select(Alert).where(Alert.title == "Live exit not completed"))).scalar_one()


async def test_the_account_limit_stops_things_once_not_every_check(db_session):
    deployment, inst = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    db_session.add(LiveRiskSettings(user_id=deployment.owner_id, account_daily_loss_limit=500.0))
    await db_session.commit()
    _price(inst["short"], 110.0)
    _price(inst["hedge"], 51.0)

    async def gateway_for(_):
        return gateway

    gateway.prices.update({"NIFTYT22700CE": 110.0})
    try:
        assert await native_live.check_account_limits(db_session, gateway_for, NOW + timedelta(minutes=1)) == [deployment.owner_id]
        alerts = len((await db_session.execute(select(Alert))).scalars().all())
        orders = len(gateway.orders)
        # the realised loss is still past the limit, but nothing of theirs runs any more
        assert await native_live.check_account_limits(db_session, gateway_for, NOW + timedelta(minutes=2)) == []
    finally:
        tick_engine.forget([inst["short"].id, inst["hedge"].id])
    assert len((await db_session.execute(select(Alert))).scalars().all()) == alerts and len(gateway.orders) == orders


async def test_reconciliation_counts_what_a_paused_strategy_holds(db_session):
    deployment, inst = await _setup(db_session)
    gateway = FakeGateway()
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    paused = LiveNativeDeployment(owner_id=deployment.owner_id, strategy_id=deployment.strategy_id,
                                  strategy_version_id=deployment.strategy_version_id, broker_account_id=deployment.broker_account_id,
                                  status=LIVE_NATIVE_PAUSED, capital=100000.0, pause_reason="paused by you")
    db_session.add(paused)
    await db_session.flush()
    db_session.add(LiveNativePosition(deployment_id=paused.id, instrument_id=inst["hedge"].id, quantity=65.0, avg_price=50.0,
                                      strategy_quantity=650.0, opened_at=NOW))
    await db_session.commit()
    gateway.held[("NFO", "NIFTYT22900CE")] += 65.0  # the broker holds both strategies' hedges

    assert await native_live.reconcile_account(db_session, deployment.broker_account_id, gateway, NOW) == []
    assert deployment.status == LIVE_NATIVE_ACTIVE and paused.pause_reason == "paused by you"


async def test_an_account_that_hasnt_passed_the_broker_test_runs_nothing(db_session):
    deployment, _ = await _setup(db_session)
    account = await db_session.get(BrokerAccount, deployment.broker_account_id)
    account.live_verified_at = None
    await db_session.commit()
    gateway = FakeGateway()
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert out.signal == "NOT_TESTED" and deployment.status == LIVE_NATIVE_PAUSED and gateway.orders == []
    assert "broker test" in deployment.pause_reason


async def test_your_own_holdings_are_counted_apart_from_the_strategys(db_session):
    deployment, inst = await _setup(db_session, code=STOCK, state={"want": "buy", "qty": 10})
    gateway = FakeGateway()
    gateway.held[("NSE", "SBIN")] = 100.0  # yours, from before
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert gateway.held[("NSE", "SBIN")] == 110.0
    [baseline] = (await db_session.execute(select(LiveAccountBaseline))).scalars().all()
    assert (baseline.contract_key, baseline.symbol, baseline.quantity) == ("NSE|SBIN", "SBIN", 100.0)
    assert await native_live.reconcile_account(db_session, deployment.broker_account_id, gateway, NOW) == []
    assert deployment.status == LIVE_NATIVE_ACTIVE

    gateway.held[("NSE", "SBIN")] = 60.0  # you sold 50 of yours in the broker's app
    [difference] = await native_live.reconcile_account(db_session, deployment.broker_account_id, gateway, NOW)
    assert difference == ("SBIN: the broker holds 60, the live strategies 10 and you 100 of your own -- "
                          "if you traded it yourself, that's the difference")
    assert deployment.status == LIVE_NATIVE_PAUSED


async def test_your_holdings_are_noted_only_before_the_first_strategy_trade(db_session):
    deployment, inst = await _setup(db_session, code=STOCK, state={"want": "buy", "qty": 10})
    gateway = FakeGateway()
    gateway.held[("NSE", "SBIN")] = 100.0
    await native_live.run_live_native(db_session, deployment, gateway, NOW)
    second = LiveNativeDeployment(owner_id=deployment.owner_id, strategy_id=deployment.strategy_id,
                                  strategy_version_id=deployment.strategy_version_id, broker_account_id=deployment.broker_account_id,
                                  status=LIVE_NATIVE_ACTIVE, capital=500000.0, state={"want": "buy", "qty": 5})
    db_session.add(second)
    await db_session.commit()
    await native_live.run_live_native(db_session, second, gateway, NOW + timedelta(minutes=1))
    [baseline] = (await db_session.execute(select(LiveAccountBaseline))).scalars().all()
    assert baseline.quantity == 100.0  # not re-read as 110 while the first strategy holds it
    assert await native_live.reconcile_account(db_session, deployment.broker_account_id, gateway, NOW) == []


async def test_an_account_it_cant_read_before_a_first_trade_sends_nothing(db_session):
    deployment, _ = await _setup(db_session, code=STOCK, state={"want": "buy", "qty": 10})
    gateway = FakeGateway()

    async def down():
        raise RuntimeError("broker down")

    gateway.net_positions = down
    out = await native_live.run_live_native(db_session, deployment, gateway, NOW)
    assert out.signal == "BASELINE" and gateway.orders == [] and deployment.status == LIVE_NATIVE_PAUSED
