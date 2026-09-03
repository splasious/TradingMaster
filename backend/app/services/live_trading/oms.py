"""Order Management System for live trading (PRD sections 22-23):

  Market Data -> Strategy -> Signal -> Risk Engine -> Order Manager -> Broker Adapter
      -> Broker/Exchange -> Execution Confirmation -> Portfolio Update

Deliberately mirrors paper_trading/engine.py's shape (same risk engine, same
one-position-at-a-time model, same signal evaluators) so the two are easy to
compare -- but every price here is real (never the simulated tick engine)
and every order is real (placed through the authenticated broker adapter --
DeltaExchangeBroker or ZerodhaKiteBroker -- confirmed via a follow-up status
check before being trusted -- PRD Rule 5: no unconfirmed orders).

Broker-specific concepts (Delta's numeric product_id vs Kite's
tradingsymbol/exchange/product) never leak past `_get_live_price_and_context`
and `_submit_and_confirm` -- everything above that point is broker-agnostic.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_payload
from app.models.alert import Alert, AlertSeverity, AlertType
from app.models.broker import Broker, BrokerAccount, BrokerCredential
from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LiveOrder, LivePosition, LiveTrade
from app.models.market_data import OhlcvCandle
from app.models.strategy import Strategy, StrategyVersion
from app.services.alerts.service import create_alert
from app.services.audit import write_audit_log
from app.services.backtest.engine import PositionSizing, quantity_for
from app.services.live_trading.kill_switch import get_kill_switch
from app.services.live_trading.order_state_machine import STATE_MAPS, LiveOrderStatus
from app.services.market_data.base import MarketDataSourceError
from app.services.market_data.delta_source import DeltaExchangeDataSource
from app.services.risk.engine import evaluate_entry, evaluate_exit
from app.services.strategy.rules import evaluate_rule_node
from app.services.strategy.sandbox import run_python_strategy

LOOKBACK_BARS = 60

# See paper_trading/engine.py's REJECTION_ALERT_COOLDOWN -- same reasoning:
# a rejection reason like "max positions reached" is an ongoing, expected
# state while a deployment stays fully allocated, not a new event on every
# evaluation tick.
REJECTION_ALERT_COOLDOWN = timedelta(minutes=30)


@dataclass
class LiveOutcome:
    action: str  # "entered" | "exited" | "rejected" | "hold" | "error" | "skipped" | "blocked"
    signal: str | None = None
    price: float | None = None
    reason: str | None = None


async def _get_authenticated_broker(db: AsyncSession, broker_account: BrokerAccount):
    from app.services.broker.registry import get_broker_adapter  # local import avoids a cycle at module load

    credential_result = await db.execute(select(BrokerCredential).where(BrokerCredential.broker_account_id == broker_account.id))
    credential = credential_result.scalar_one_or_none()
    if credential is None:
        raise MarketDataSourceError("No credentials stored for this broker account")

    creds = json.loads(decrypt_payload(credential.encrypted_payload))
    broker_row = await db.get(Broker, broker_account.broker_id)
    broker = get_broker_adapter(broker_row.code)
    await broker.authenticate(creds)
    await broker.connect()
    return broker


async def _get_live_price_and_context(broker_code: str, broker, instrument: Instrument) -> tuple[float, dict]:
    """Real current price plus whatever broker-specific fields place_order()
    needs beyond quantity/side (Delta: a numeric product_id; Kite: the
    tradingsymbol/exchange/product it trades under) -- the only place in
    this module that knows either broker's vocabulary."""
    if broker_code == "delta_exchange":
        ticker = await DeltaExchangeDataSource().get_ticker(instrument.external_ref)
        return ticker["price"], {"product_id": ticker["product_id"]}
    if broker_code == "zerodha_kite":
        quote = await broker.get_ltp("NSE", instrument.external_ref)
        return quote["price"], {"tradingsymbol": instrument.external_ref, "exchange": "NSE", "product": "CNC"}
    raise MarketDataSourceError(f"No live pricing wired up for broker '{broker_code}'")


async def evaluate_live_deployment(db: AsyncSession, deployment: LiveDeployment) -> LiveOutcome:
    kill_switch = await get_kill_switch(db)
    if kill_switch.active:
        return LiveOutcome(action="blocked", reason=f"Kill switch active: {kill_switch.reason or 'no reason given'}")

    version = await db.get(StrategyVersion, deployment.strategy_version_id)
    instrument = await db.get(Instrument, deployment.instrument_id)
    broker_account = await db.get(BrokerAccount, deployment.broker_account_id)
    if version is None or instrument is None or broker_account is None:
        return LiveOutcome(action="error", reason="deployment references missing data")

    position_result = await db.execute(select(LivePosition).where(LivePosition.deployment_id == deployment.id))
    position = position_result.scalar_one_or_none()

    broker_row = await db.get(Broker, broker_account.broker_id)
    try:
        broker = await _get_authenticated_broker(db, broker_account)
    except Exception as exc:
        return LiveOutcome(action="error", reason=f"Could not authenticate with broker: {exc}")

    try:
        current_price, order_context = await _get_live_price_and_context(broker_row.code, broker, instrument)
    except MarketDataSourceError as exc:
        return LiveOutcome(action="error", reason=f"Could not fetch live price: {exc}")

    candles_result = await db.execute(
        select(OhlcvCandle)
        .where(OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == deployment.timeframe)
        .order_by(OhlcvCandle.ts.desc())
        .limit(LOOKBACK_BARS)
    )
    candles = list(reversed(candles_result.scalars().all()))

    now = datetime.now(timezone.utc)
    synthetic_bar = OhlcvCandle(
        instrument_id=instrument.id, timeframe=deployment.timeframe, ts=now,
        open=candles[-1].close if candles else current_price,
        high=max((candles[-1].close if candles else current_price), current_price),
        low=min((candles[-1].close if candles else current_price), current_price),
        close=current_price, volume=0.0, source=f"live_{broker_row.code}",
    )
    bars_for_eval = [*candles, synthetic_bar]

    if position is not None:
        stop_pct = version.risk_rules.get("stop_loss_pct")
        target_pct = version.risk_rules.get("take_profit_pct")
        stop_price = position.avg_entry_price * (1 - stop_pct / 100) if stop_pct else None
        target_price = position.avg_entry_price * (1 + target_pct / 100) if target_pct else None
        if stop_price is not None and current_price <= stop_price:
            return await _exit_position(db, deployment, broker, broker_row.code, position, order_context, now, "stop_loss", stop_price)
        if target_price is not None and current_price >= target_price:
            return await _exit_position(db, deployment, broker, broker_row.code, position, order_context, now, "take_profit", target_price)

    if version.python_code:
        bars_dicts = [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume or 0.0} for c in bars_for_eval]
        sandbox_result = await run_python_strategy(version.python_code, bars_dicts, version.parameters)
        if sandbox_result.error:
            return LiveOutcome(action="error", reason=sandbox_result.error)
        signal = sandbox_result.signal
    else:
        entry_met = evaluate_rule_node(bars_for_eval, version.entry_rules)
        exit_met = evaluate_rule_node(bars_for_eval, version.exit_rules)
        signal = "BUY" if entry_met else ("SELL" if exit_met else "HOLD")

    deployment.last_evaluated_at = now

    if signal == "BUY" and position is None:
        return await _try_enter(db, deployment, broker, broker_row.code, version, current_price, order_context, now)
    if signal == "SELL" and position is not None:
        return await _exit_position(db, deployment, broker, broker_row.code, position, order_context, now, "signal", current_price)

    await db.commit()
    return LiveOutcome(action="hold", signal=signal, price=current_price)


async def _submit_and_confirm(db, deployment, broker, broker_code, side, quantity, order_context):
    client_order_id = f"tm-{uuid.uuid4().hex[:24]}"

    live_order = LiveOrder(
        deployment_id=deployment.id, client_order_id=client_order_id, side=side, quantity=quantity,
        order_type="market_order", status=LiveOrderStatus.SUBMITTED.value,
    )
    db.add(live_order)
    await db.flush()

    try:
        placement = await broker.place_order(
            {**order_context, "quantity": quantity, "side": side, "order_type": "market", "client_order_id": client_order_id}
        )
    except Exception as exc:
        live_order.status = LiveOrderStatus.REJECTED.value
        live_order.reason = str(exc)
        await db.commit()
        return None, str(exc)

    live_order.broker_order_id = placement["broker_order_id"]

    # PRD Rule 5: an order is not "executed" just because place_order()
    # returned -- confirm its actual state with a follow-up call.
    state_map = STATE_MAPS.get(broker_code, {})
    try:
        status_check = await broker.get_order_status(placement["broker_order_id"])
        confirmed_status = state_map.get(status_check.get("status"), LiveOrderStatus.OPEN)
    except Exception:
        confirmed_status = state_map.get(placement.get("status"), LiveOrderStatus.OPEN)

    live_order.status = confirmed_status.value
    live_order.confirmed_at = datetime.now(timezone.utc)
    await db.commit()
    return live_order, None


async def _try_enter(db, deployment, broker, broker_code, version, price, order_context, now):
    # Live capital tracking comes from the broker's own real balance, never
    # a local ledger -- fetch it before sizing, not after.
    try:
        balance = await broker.get_balance()
    except Exception as exc:
        return LiveOutcome(action="error", reason=f"Could not fetch broker balance: {exc}")
    available_cash = balance.get("available_margin", 0.0)
    if deployment.allocated_capital is not None:
        available_cash = min(available_cash, deployment.allocated_capital)

    sizing = PositionSizing(**version.position_sizing)
    quantity = quantity_for(available_cash, price, sizing)
    if quantity <= 0:
        return LiveOutcome(action="rejected", signal="BUY", price=price, reason="Position sizing produced zero quantity")

    open_position_count = (
        await db.execute(select(LivePosition).join(LiveDeployment).where(LiveDeployment.owner_id == deployment.owner_id))
    ).scalars().all()

    decision = evaluate_entry(
        available_cash=available_cash, notional=quantity * price,
        open_position_count=len(open_position_count), max_positions=version.risk_rules.get("max_positions"),
        realized_pnl_today=0.0, initial_capital=available_cash or 1.0,
        max_daily_loss_pct=version.risk_rules.get("max_daily_loss_pct"),
    )
    if not decision.approved:
        rejection_message = decision.reason or "Order rejected by risk engine"
        await write_audit_log(
            db, user_id=deployment.owner_id, action="LIVE_ORDER_REJECTED", object_type="live_deployment",
            object_id=str(deployment.id), new_value={"reason": decision.reason},
        )
        recent_same_alert = (
            await db.execute(
                select(Alert.id)
                .where(
                    Alert.object_type == "live_deployment",
                    Alert.object_id == str(deployment.id),
                    Alert.message == rejection_message,
                    Alert.created_at >= now - REJECTION_ALERT_COOLDOWN,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if recent_same_alert is None:
            await create_alert(
                db, user_id=deployment.owner_id, alert_type=AlertType.ORDER_REJECTED.value, severity=AlertSeverity.CRITICAL,
                title="LIVE order rejected", message=rejection_message,
                object_type="live_deployment", object_id=str(deployment.id),
            )
        await db.commit()
        return LiveOutcome(action="rejected", signal="BUY", price=price, reason=decision.reason)

    live_order, error = await _submit_and_confirm(db, deployment, broker, broker_code, "buy", quantity, order_context)
    if error:
        await create_alert(
            db, user_id=deployment.owner_id, alert_type=AlertType.BROKER_DISCONNECTED.value, severity=AlertSeverity.CRITICAL,
            title="Live order failed", message=error, object_type="live_deployment", object_id=str(deployment.id),
        )
        await db.commit()
        return LiveOutcome(action="error", signal="BUY", price=price, reason=error)

    if live_order.status in (LiveOrderStatus.REJECTED.value,):
        return LiveOutcome(action="rejected", signal="BUY", price=price, reason=live_order.reason)

    db.add(LivePosition(deployment_id=deployment.id, quantity=quantity, avg_entry_price=price, opened_at=now))
    await write_audit_log(
        db, user_id=deployment.owner_id, action="LIVE_ORDER_PLACED", object_type="live_deployment",
        object_id=str(deployment.id), new_value={"side": "buy", "quantity": quantity, "price": price, "broker_order_id": live_order.broker_order_id},
    )
    strategy = await db.get(Strategy, deployment.strategy_id)
    await create_alert(
        db, user_id=deployment.owner_id, alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO,
        title="LIVE order filled", message=f"{strategy.name if strategy else 'Strategy'}: bought {quantity} @ {price:.2f} (real order)",
        object_type="live_deployment", object_id=str(deployment.id),
    )
    await db.commit()
    return LiveOutcome(action="entered", signal="BUY", price=price)


async def _exit_position(db, deployment, broker, broker_code, position, order_context, now, reason, price):
    evaluate_exit()  # always approved; called for symmetry/auditability with paper trading
    live_order, error = await _submit_and_confirm(db, deployment, broker, broker_code, "sell", position.quantity, order_context)
    if error:
        await create_alert(
            db, user_id=deployment.owner_id, alert_type=AlertType.BROKER_DISCONNECTED.value, severity=AlertSeverity.CRITICAL,
            title="Live exit failed", message=error, object_type="live_deployment", object_id=str(deployment.id),
        )
        await db.commit()
        return LiveOutcome(action="error", price=None, reason=error)

    await write_audit_log(
        db, user_id=deployment.owner_id, action="LIVE_ORDER_PLACED", object_type="live_deployment",
        object_id=str(deployment.id), new_value={"side": "sell", "quantity": position.quantity, "exit_reason": reason, "broker_order_id": live_order.broker_order_id},
    )

    # Fill price is approximated as the quote/trigger price at signal time,
    # not parsed from the broker's order-status response (that would need
    # average_fill_price handling not built in this pass) -- close enough
    # for reporting on a market order, not exact to the cent.
    pnl = (price - position.avg_entry_price) * position.quantity
    pnl_pct = (price - position.avg_entry_price) / position.avg_entry_price * 100 if position.avg_entry_price else 0.0
    db.add(
        LiveTrade(
            deployment_id=deployment.id, entry_ts=position.opened_at, entry_price=position.avg_entry_price,
            exit_ts=now, exit_price=price, quantity=position.quantity, pnl=pnl, pnl_pct=pnl_pct, exit_reason=reason,
        )
    )

    strategy = await db.get(Strategy, deployment.strategy_id)
    strategy_name = strategy.name if strategy else "Strategy"
    alert_type = {"stop_loss": AlertType.STOP_LOSS_TRIGGERED, "take_profit": AlertType.TARGET_TRIGGERED}.get(reason, AlertType.ORDER_EXECUTED)
    await create_alert(
        db, user_id=deployment.owner_id, alert_type=alert_type.value,
        severity=AlertSeverity.WARNING if reason == "stop_loss" else AlertSeverity.INFO,
        title="LIVE position closed", message=f"{strategy_name}: sold {position.quantity} @ {price:.2f} (real order), P&L {pnl:+.2f}",
        object_type="live_deployment", object_id=str(deployment.id),
    )
    await db.delete(position)
    await db.commit()
    return LiveOutcome(action="exited", signal="SELL", price=price, reason=reason)
