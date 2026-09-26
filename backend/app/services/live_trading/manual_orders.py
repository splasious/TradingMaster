"""Manual (deployment-free) live order placement -- fires a single real
broker order directly from a UI action (e.g. a Market Scanner result row),
with no strategy, StrategyVersion, or LiveDeployment behind it. Shares the
`live_orders` table and the same confirm-after-place discipline as the
deployment-driven OMS (oms.py's `_submit_and_confirm`), but is deliberately
a separate, smaller code path -- a manual order has no signal, no
`version.risk_rules`, and no position to track, so oms.py's entry/exit
pipeline doesn't apply here. See oms.py's own module docstring for why
`get_authenticated_broker`/`get_live_price_and_context` are exported for
reuse here rather than duplicated.

Safety gates enforced below, in this order -- every one fails closed:
  1. `confirmed` must be True (re-checked server-side, PRD section 49 --
     same rule DeploymentCreate.confirmed already follows).
  2. Kill switch must be inactive.
  3. `broker_account.user_id` must be the caller's (or caller is admin).
  4. `broker_account.environment` must be "live" -- never fire a manual
     real order against a paper account; paper trading has its own flows.
  5. `broker_account.connection.status` must be "connected".
  6. For F&O instruments, `quantity` must be an exact multiple of lot_size.
  7. Order notional (quantity * live price) must not exceed
     `settings.manual_order_max_notional`.
  8. Order notional must not exceed the broker's own available margin.

No `LiveOrder` row is created for a rejection at any of the above -- a row
only exists once we're past every pre-check and about to call
`broker.place_order`, mirroring `_submit_and_confirm`'s own
create-then-place ordering.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.alert import AlertSeverity, AlertType
from app.models.broker import Broker, BrokerAccount, ConnectionStatus, Environment
from app.models.instrument import Instrument
from app.models.live_trading import LiveOrder
from app.models.user import User
from app.services.alerts.service import create_alert
from app.services.audit import write_audit_log
from app.services.broker.registry import supports_trading
from app.services.live_trading.kill_switch import get_kill_switch
from app.services.live_trading.oms import get_authenticated_broker, get_live_price_and_context
from app.services.live_trading.order_state_machine import STATE_MAPS, LiveOrderStatus

_FNO_TYPES = ("option", "future")


class ManualOrderError(Exception):
    """Raised for every rejection reason above -- the endpoint maps this to
    an HTTP 400 with the message as detail, the same "specific, auditable
    reason" convention the risk engine's RiskDecision already follows."""


async def place_manual_order(
    db: AsyncSession, user: User, *, instrument_id: str, broker_account_id: str,
    side: str, quantity: float, order_type: str, limit_price: float | None,
    product: str, confirmed: bool,
) -> LiveOrder:
    if not confirmed:
        raise ManualOrderError("Manual order requires explicit confirmation (confirmed=true).")

    kill_switch = await get_kill_switch(db)
    if kill_switch.active:
        raise ManualOrderError(f"Kill switch active: {kill_switch.reason or 'no reason given'}")

    instrument = await db.get(Instrument, uuid.UUID(instrument_id))
    broker_account = await db.get(BrokerAccount, uuid.UUID(broker_account_id))
    if instrument is None or broker_account is None:
        raise ManualOrderError("Instrument or broker account not found")
    if broker_account.user_id != user.id:
        raise ManualOrderError("Not your broker account")
    if broker_account.environment != Environment.LIVE.value:
        raise ManualOrderError("Manual orders are only allowed on a 'live' environment broker account")

    await db.refresh(broker_account, attribute_names=["connection"])
    connection = broker_account.connection
    if connection is None or connection.status != ConnectionStatus.CONNECTED.value:
        raise ManualOrderError("Broker account is not connected")

    if instrument.instrument_type in _FNO_TYPES and instrument.lot_size:
        if quantity % instrument.lot_size != 0:
            raise ManualOrderError(f"Quantity must be a multiple of the lot size ({instrument.lot_size})")

    broker_row = await db.get(Broker, broker_account.broker_id)
    if not supports_trading(broker_row.code):
        raise ManualOrderError(f"{broker_row.name} is connected for login and funds only -- trading through it isn't enabled yet")
    try:
        broker = await get_authenticated_broker(db, broker_account)
    except Exception as exc:
        raise ManualOrderError(f"Could not authenticate with broker: {exc}") from exc

    try:
        current_price, order_context = await get_live_price_and_context(broker_row.code, broker, instrument, product)
    except Exception as exc:
        raise ManualOrderError(f"Could not fetch live price: {exc}") from exc

    settings = get_settings()
    notional = quantity * current_price
    if notional > settings.manual_order_max_notional:
        raise ManualOrderError(
            f"Order notional {notional:.2f} exceeds the manual-order cap ({settings.manual_order_max_notional:.2f})"
        )

    try:
        balance = await broker.get_balance()
    except Exception as exc:
        raise ManualOrderError(f"Could not fetch broker balance: {exc}") from exc
    available_margin = balance.get("available_margin", 0.0)
    if notional > available_margin:
        raise ManualOrderError(f"Insufficient margin: order notional {notional:.2f} exceeds available margin {available_margin:.2f}")

    client_order_id = f"tm-manual-{uuid.uuid4().hex[:18]}"
    live_order = LiveOrder(
        deployment_id=None, instrument_id=instrument.id, broker_account_id=broker_account.id, owner_id=user.id,
        client_order_id=client_order_id, side=side, quantity=quantity,
        order_type="market_order" if order_type == "market" else "limit_order",
        status=LiveOrderStatus.SUBMITTED.value, product=product,
    )
    db.add(live_order)
    await db.flush()

    try:
        placement = await broker.place_order({
            **order_context, "quantity": quantity, "side": side, "order_type": order_type,
            "limit_price": limit_price, "client_order_id": client_order_id,
        })
    except Exception as exc:
        live_order.status = LiveOrderStatus.REJECTED.value
        live_order.reason = str(exc)
        await write_audit_log(
            db, user_id=user.id, action="LIVE_MANUAL_ORDER_REJECTED", object_type="live_order",
            object_id=str(live_order.id), new_value={"instrument": instrument.symbol, "reason": str(exc)},
        )
        await create_alert(
            db, user_id=user.id, alert_type=AlertType.ORDER_REJECTED.value, severity=AlertSeverity.CRITICAL,
            title="Manual order rejected", message=str(exc), object_type="live_order", object_id=str(live_order.id),
        )
        await db.commit()
        return live_order

    live_order.broker_order_id = placement["broker_order_id"]

    # PRD Rule 5: not "executed" just because place_order() returned --
    # confirm its actual state with a follow-up call, same discipline as
    # oms.py's _submit_and_confirm.
    state_map = STATE_MAPS.get(broker_row.code, {})
    try:
        status_check = await broker.get_order_status(placement["broker_order_id"])
        confirmed_status = state_map.get(status_check.get("status"), LiveOrderStatus.OPEN)
    except Exception:
        confirmed_status = state_map.get(placement.get("status"), LiveOrderStatus.OPEN)

    live_order.status = confirmed_status.value
    live_order.confirmed_at = datetime.now(timezone.utc)

    await write_audit_log(
        db, user_id=user.id, action="LIVE_MANUAL_ORDER_PLACED", object_type="live_order", object_id=str(live_order.id),
        new_value={
            "instrument": instrument.symbol, "side": side, "quantity": quantity, "product": product,
            "broker_order_id": live_order.broker_order_id,
        },
    )
    await create_alert(
        db, user_id=user.id, alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO,
        title="Manual order placed", message=f"{instrument.symbol}: {side} {quantity} @ ~{current_price:.2f} (real order)",
        object_type="live_order", object_id=str(live_order.id),
    )
    await db.commit()
    return live_order
