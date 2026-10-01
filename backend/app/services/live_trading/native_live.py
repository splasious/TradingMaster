"""Native strategies trading live -- Phase A of the design agreed on 1 Oct 2026.

A strategy's code runs exactly as on paper (paper_trading/native_runner.py);
only what its ctx.open_leg / ctx.close_leg do changes. On paper they move
cash; here they become real market orders on the broker account the
strategy was started on (native_gateway.py), sent once the strategy's
check is over, as one batch:

  - buy legs first, then sell legs -- a hedge is in before the leg it
    hedges, and a short is bought back before its hedge is sold;
  - each order waits for the broker's fill; the real fill price and
    quantity are what's kept (LiveNativePosition) and what trades are
    recorded at (LiveNativeTrade) -- not the price the strategy saw;
  - one failed leg (rejected, not filled in time, broker down): the legs of
    the batch that did open are closed at once (purpose "rollback"), the
    strategy's state goes back to what it was before the check, and you're
    alerted. If an exit leg had already gone through, or a rollback itself
    fails, the strategy is paused -- what it believes no longer matches
    what the broker holds. A failed exit closes nothing and is tried again
    at the next check. Orders failing MAX_FAILED_BATCHES_PER_DAY times in a
    day pause it rather than pay the spread again and again.

Size: an F&O leg trades `lots_per_leg` lots whatever the code's own size; a
stock trades what the strategy sizes from its live capital (LivePool). A
close is scaled to what's really held.

Before every check (run_live_native):
  - the kill switch: on, nothing runs;
  - the daily loss limit (realised today + open P&L of what's held, against
    `daily_loss_limit`, default DEFAULT_DAILY_LOSS_PCT of `capital`): hit,
    everything it holds is squared off and it pauses until the next
    session;
and before every batch:
  - at most `max_orders_per_day` orders a day: over, it pauses;
  - the capital cap: stocks and option premium paid can't take its cash
    below zero (futures and short options are left to the broker's margin
    check -- a rejection is a failed leg).
Account-wide (check_account_limits): all of a user's live strategies
together past their limit (default DEFAULT_ACCOUNT_LOSS_PCT of their live
capital) -- every one is squared off and paused, and the kill switch goes on.

Every minute (reconcile_account): what the broker holds against what the
strategies on that account hold (paused ones included) plus what you
already held of it yourself when a strategy first traded it there
(record_baselines, LiveAccountBaseline); any difference pauses every
running strategy holding that instrument, and alerts you with both sides.
A strategy only runs on a broker account that passed the broker test
(broker_test.py). The account-wide stop happens once a day: after it, with
nothing of the user's running, it isn't checked again.
"""

import copy
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import AlertSeverity, AlertType
from app.models.instrument import Instrument
from app.models.broker import BrokerAccount
from app.models.live_native import (
    LIVE_NATIVE_ACTIVE,
    LIVE_NATIVE_PAUSED,
    LiveAccountBaseline,
    LiveNativeDeployment,
    LiveNativePosition,
    LiveNativeTrade,
    LiveRiskSettings,
)
from app.models.live_trading import LiveOrder
from app.models.strategy import StrategyVersion
from app.services.alerts.service import create_alert
from app.services.backfill_platform.coverage import IST, next_trading_day
from app.services.live_trading import kill_switch
from app.services.live_trading.broker_contracts import ContractListError, ContractNotFound
from app.services.live_trading.native_gateway import Fill, product_for
from app.services.live_trading.order_state_machine import LiveOrderStatus
from app.services.market_data.live_price import live_price
from app.services.market_data.tick_engine import tick_engine
from app.services.paper_trading.native_runner import NativeContext
from app.services.paper_trading.trade_record import estimate_charges, resolve_leg_details

logger = logging.getLogger(__name__)

DEFAULT_DAILY_LOSS_PCT = 3.0
DEFAULT_ACCOUNT_LOSS_PCT = 5.0
MAX_FAILED_BATCHES_PER_DAY = 2
FNO_TYPES = ("option", "future")
SESSION_OPEN = time(9, 15)
_QTY_EPS = 1e-6


class LiveOrderError(Exception):
    """A leg the strategy asked for can't be turned into a real order."""


@dataclass
class Intent:
    kind: str  # "open" | "close"
    instrument: Instrument
    side: str  # "buy" | "sell"
    quantity: float  # live quantity
    strategy_quantity: float  # the strategy's own
    ref_price: float


@dataclass
class LiveOutcome:
    action: str
    signal: str | None = None
    reason: str | None = None
    wake_at: datetime | None = None
    orders: list[str] = field(default_factory=list)


class LivePool:
    """Stands in for a paper pool: the strategy's live capital, less what
    it has paid for stocks and long options (plus what short options paid
    it), plus everything it has realised."""

    def __init__(self, user_id: uuid.UUID, cash: float) -> None:
        self.user_id = user_id
        self.cash = cash


def _is_fno(instrument: Instrument) -> bool:
    return instrument.instrument_type in FNO_TYPES


def _session_start_utc(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(IST).date(), time(0, 0), tzinfo=IST).astimezone(timezone.utc)


class LiveNativeContext(NativeContext):
    """NativeContext whose legs are collected as order intents instead of
    moving cash; native_live executes them after the strategy's check."""

    is_live = True

    def __init__(self, db: AsyncSession, deployment: LiveNativeDeployment, pool: LivePool, state: dict,
                 positions: dict[uuid.UUID, LiveNativePosition], now: datetime | None = None):
        super().__init__(db=db, portfolio=pool, deployment=deployment, state=state, now=now)
        self.positions = positions
        self.intents: list[Intent] = []
        self.trade_intents: list[dict] = []

    def _lots(self, instrument: Instrument) -> int:
        if not instrument.lot_size:
            raise LiveOrderError(f"{instrument.symbol}: lot size unknown, can't size a live order")
        return int(instrument.lot_size)

    def _live_quantity(self, instrument: Instrument, quantity: float) -> float:
        if _is_fno(instrument):
            return float(self.deployment.lots_per_leg * self._lots(instrument))
        return float(int(quantity))

    def _move(self, instrument: Instrument, side: str, quantity: float, price: float) -> None:
        if instrument.instrument_type != "future":  # a future books only its P&L
            self.portfolio.cash += quantity * price if side == "sell" else -quantity * price

    async def open_leg(self, instrument: Instrument, side: str, quantity: float, price: float) -> None:
        live_qty = self._live_quantity(instrument, quantity)
        if live_qty <= 0:
            raise LiveOrderError(f"{instrument.symbol}: a {side} of {quantity:g} is less than one share or lot live")
        self.intents.append(Intent("open", instrument, side, live_qty, float(quantity), price))
        self._move(instrument, side, live_qty, price)

    async def close_leg(self, instrument: Instrument, side: str, quantity: float, price: float, entry_price: float | None = None) -> None:
        held = self.positions.get(instrument.id)
        if held is None or abs(held.quantity) < _QTY_EPS:
            raise LiveOrderError(f"the strategy closes {instrument.symbol}, which it doesn't hold live")
        if (held.quantity > 0) != (side == "sell"):
            raise LiveOrderError(f"the strategy {side}s to close {instrument.symbol}, but holds it {'long' if held.quantity > 0 else 'short'}")
        fraction = quantity / held.strategy_quantity if held.strategy_quantity else 1.0
        live_qty = abs(held.quantity)
        if fraction < 0.999:
            step = self._lots(instrument) if _is_fno(instrument) else 1
            live_qty = max(step, (abs(held.quantity) * fraction) // step * step)
        self.intents.append(Intent("close", instrument, side, float(live_qty), float(quantity), price))
        self._move(instrument, side, live_qty, price)

    async def record_trade(self, legs: list[dict], pnl: float, pnl_pct: float, exit_reason: str, opened_at: datetime,
                           closed_at: datetime | None = None, alert: bool = True) -> None:
        """Kept for after the orders: the trade is recorded at the broker's
        real fills, not the strategy's own figures."""
        self.trade_intents.append({
            "instrument_ids": [str(leg.get("instrument_id")) for leg in legs], "exit_reason": exit_reason, "opened_at": opened_at,
        })


# --------------------------------------------------------------- helpers --

async def load_positions(db: AsyncSession, deployment: LiveNativeDeployment) -> dict[uuid.UUID, LiveNativePosition]:
    rows = (await db.execute(select(LiveNativePosition).where(LiveNativePosition.deployment_id == deployment.id))).scalars().all()
    return {row.instrument_id: row for row in rows}


async def _realised(db: AsyncSession, deployment_id: uuid.UUID, since: datetime | None = None) -> float:
    stmt = select(func.coalesce(func.sum(LiveNativeTrade.pnl), 0.0)).where(LiveNativeTrade.deployment_id == deployment_id)
    if since is not None:
        stmt = stmt.where(LiveNativeTrade.closed_at >= since)
    return float((await db.execute(stmt)).scalar_one() or 0.0)


async def pool_cash(db: AsyncSession, deployment: LiveNativeDeployment, positions: dict[uuid.UUID, LiveNativePosition]) -> float:
    cash = deployment.capital + await _realised(db, deployment.id)
    for position in positions.values():
        instrument = await db.get(Instrument, position.instrument_id)
        if instrument is not None and instrument.instrument_type != "future":
            cash -= position.quantity * position.avg_price  # long: paid; short: received
    return cash


async def _mark(db: AsyncSession, instrument: Instrument, now: datetime) -> float | None:
    return await live_price(db, instrument, now) or tick_engine.get_current_price(instrument.id)


async def day_pnl(db: AsyncSession, deployment: LiveNativeDeployment, now: datetime) -> float:
    """Realised today plus the open P&L of everything it holds."""
    total = await _realised(db, deployment.id, _session_start_utc(now))
    for position in (await load_positions(db, deployment)).values():
        instrument = await db.get(Instrument, position.instrument_id)
        price = await _mark(db, instrument, now) if instrument is not None else None
        if price is not None:
            total += (price - position.avg_price) * position.quantity
    return total


def loss_limit(deployment: LiveNativeDeployment) -> float:
    return deployment.daily_loss_limit if deployment.daily_loss_limit is not None else deployment.capital * DEFAULT_DAILY_LOSS_PCT / 100


async def _alert(db: AsyncSession, deployment: LiveNativeDeployment, severity: AlertSeverity, alert_type: AlertType, title: str, message: str) -> None:
    await create_alert(
        db, user_id=deployment.owner_id, alert_type=alert_type.value, severity=severity, title=title[:200], message=message[:1000],
        object_type="live_native_deployment", object_id=str(deployment.id),
    )
    if severity != AlertSeverity.INFO:
        try:
            from app.services.notifications.telegram import send_telegram
            from app.services.paper_trading.native_runner import _is_administrator

            if await _is_administrator(db, deployment.owner_id):
                await send_telegram(title, message)
        except Exception:
            logger.exception("Telegram alert for live deployment %s failed", deployment.id)


async def pause(db: AsyncSession, deployment: LiveNativeDeployment, reason: str, now: datetime, resume_at: datetime | None = None,
                severity: AlertSeverity = AlertSeverity.CRITICAL) -> None:
    deployment.status = LIVE_NATIVE_PAUSED
    deployment.pause_reason = reason[:500]
    deployment.paused_at = now
    deployment.resume_at = resume_at
    await _alert(db, deployment, severity, AlertType.STRATEGY_STOPPED, "Live strategy paused", reason)


def _next_session_open(now: datetime) -> datetime:
    return datetime.combine(next_trading_day(now.astimezone(IST).date()), SESSION_OPEN, tzinfo=IST).astimezone(timezone.utc)


# ------------------------------------------------------------- execution --

async def _send(db: AsyncSession, deployment: LiveNativeDeployment, gateway, intent: Intent, purpose: str, now: datetime) -> Fill:
    product = product_for(intent.instrument, deployment.product_style)
    client_order_id = f"tmn-{uuid.uuid4().hex[:20]}"
    order = LiveOrder(
        native_deployment_id=deployment.id, instrument_id=intent.instrument.id, broker_account_id=deployment.broker_account_id,
        owner_id=deployment.owner_id, client_order_id=client_order_id, side=intent.side, quantity=intent.quantity,
        order_type="protected_limit", status=LiveOrderStatus.SUBMITTED.value, product=product, purpose=purpose, created_at=now,
    )
    db.add(order)
    await db.flush()
    # The limit is set off the live price now, not the price the strategy saw.
    price = await _mark(db, intent.instrument, now) or intent.ref_price
    fill = await gateway.order(intent.instrument, intent.side, intent.quantity, product, client_order_id, price)
    order.limit_price = fill.limit_price
    order.broker_order_id = fill.broker_order_id
    order.status = fill.status.value
    order.filled_quantity = fill.filled_quantity
    order.average_price = fill.average_price
    order.reason = fill.reason
    order.confirmed_at = datetime.now(timezone.utc)
    await db.flush()
    return fill


async def _apply(db: AsyncSession, deployment: LiveNativeDeployment, positions: dict, intent: Intent, fill: Fill, now: datetime) -> None:
    """The broker's fill onto what the strategy holds there."""
    qty = fill.filled_quantity
    if qty <= _QTY_EPS:
        return
    price = fill.average_price if fill.average_price is not None else intent.ref_price
    signed = qty if intent.side == "buy" else -qty
    share = intent.strategy_quantity * (qty / intent.quantity) if intent.quantity else 0.0
    position = positions.get(intent.instrument.id)
    if position is None:
        position = LiveNativePosition(
            deployment_id=deployment.id, instrument_id=intent.instrument.id, quantity=signed, avg_price=price,
            strategy_quantity=share if intent.kind == "open" else 0.0, product=product_for(intent.instrument, deployment.product_style),
            opened_at=now,
        )
        db.add(position)
        positions[intent.instrument.id] = position
        return
    old = position.quantity
    new = old + signed
    if abs(old) < _QTY_EPS or (old > 0) == (signed > 0):  # adding to it
        position.avg_price = (abs(old) * position.avg_price + qty * price) / (abs(old) + qty)
    elif (old > 0) != (new > 0) and abs(new) > _QTY_EPS:  # through zero: the rest is a new position
        position.avg_price = price
    position.quantity = new
    position.strategy_quantity = max(0.0, position.strategy_quantity + (share if intent.kind == "open" else -share))
    if abs(new) < _QTY_EPS:
        positions.pop(intent.instrument.id, None)
        await db.delete(position)


async def _save_trade(db: AsyncSession, deployment: LiveNativeDeployment, closes: list[tuple[Intent, Fill, float, float]],
                      exit_reason: str, opened_at: datetime, now: datetime) -> float:
    """One trade for these closes: (intent, fill, entry avg price, quantity held before -- its sign is the side)."""
    legs, pnl, notional = [], 0.0, 0.0
    for intent, fill, entry, held in closes:
        exit_price = fill.average_price if fill.average_price is not None else intent.ref_price
        qty = fill.filled_quantity
        long = held > 0
        pnl += ((exit_price - entry) if long else (entry - exit_price)) * qty
        notional += entry * qty
        legs.append({
            "instrument_id": str(intent.instrument.id), "side": "long" if long else "short", "quantity": qty,
            "entry_price": entry, "exit_price": exit_price,
        })
    opened = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=timezone.utc)
    charges = None
    try:
        [legs] = await resolve_leg_details(db, [legs])
        charges = estimate_charges(legs, opened, now)
    except Exception:
        logger.exception("Could not resolve live trade details for %s", deployment.id)
    db.add(LiveNativeTrade(
        deployment_id=deployment.id, opened_at=opened.astimezone(timezone.utc), closed_at=now, legs=legs, pnl=pnl,
        pnl_pct=(pnl / notional * 100) if notional else 0.0, charges=charges, exit_reason=exit_reason[:64],
    ))
    return pnl


def _opposite(side: str) -> str:
    return "sell" if side == "buy" else "buy"


async def execute_batch(db: AsyncSession, deployment: LiveNativeDeployment, gateway, ctx: LiveNativeContext,
                        snapshot: dict, now: datetime) -> LiveOutcome:
    positions = ctx.positions
    held_before = {iid: (p.quantity, p.avg_price, p.opened_at) for iid, p in positions.items()}
    ordered = sorted(ctx.intents, key=lambda i: 0 if i.side == "buy" else 1)
    executed: list[tuple[Intent, Fill]] = []
    failed: tuple[Intent, Fill] | None = None
    for intent in ordered:
        fill = await _send(db, deployment, gateway, intent, intent.kind, now)
        if fill.filled_quantity > _QTY_EPS:
            executed.append((intent, fill))
        if not fill.complete or fill.filled_quantity + _QTY_EPS < intent.quantity:
            failed = (intent, fill)
            break
    for intent, fill in executed:
        await _apply(db, deployment, positions, intent, fill, now)

    if failed is None:
        closes = {intent.instrument.id: (intent, fill) for intent, fill in executed if intent.kind == "close"}
        covered: set = set()
        for trade in ctx.trade_intents:
            legs = []
            for iid in trade["instrument_ids"]:
                try:
                    key = uuid.UUID(iid)
                except ValueError:
                    continue
                if key in closes and key in held_before:
                    intent, fill = closes[key]
                    legs.append((intent, fill, held_before[key][1], held_before[key][0]))
                    covered.add(key)
            if legs:
                await _save_trade(db, deployment, legs, trade["exit_reason"], trade["opened_at"], now)
        for key, (intent, fill) in closes.items():  # a close the strategy didn't record a trade for
            if key not in covered and key in held_before:
                await _save_trade(db, deployment, [(intent, fill, held_before[key][1], held_before[key][0])], "closed",
                                  held_before[key][2], now)
        deployment.state = ctx.state
        return LiveOutcome(action="traded", orders=[f"{i.side} {i.quantity:g} {i.instrument.symbol} @ {f.average_price}" for i, f in executed])

    # --- A leg failed: close what this batch opened, go back to before. ----
    intent, fill = failed
    problem = f"{intent.side} {intent.quantity:g} {intent.instrument.symbol} failed: {fill.reason or fill.status.value}"
    rollback_failed = []
    for opened, opened_fill in reversed(executed):
        if opened.kind != "open":
            continue
        back = Intent("close", opened.instrument, _opposite(opened.side), opened_fill.filled_quantity,
                      opened.strategy_quantity * opened_fill.filled_quantity / opened.quantity, opened_fill.average_price or opened.ref_price)
        entry = opened_fill.average_price or opened.ref_price
        rb = await _send(db, deployment, gateway, back, "rollback", now)
        await _apply(db, deployment, positions, back, rb, now)
        if rb.filled_quantity > _QTY_EPS:
            await _save_trade(db, deployment, [(back, rb, entry, opened_fill.filled_quantity if opened.side == "buy" else -opened_fill.filled_quantity)],
                              "leg_failed_rollback", now, now)
        if not rb.complete or rb.filled_quantity + _QTY_EPS < back.quantity:
            rollback_failed.append(f"{opened.instrument.symbol} ({rb.reason or rb.status.value})")
    deployment.state = snapshot
    exits_done = [i.instrument.symbol for i, _ in executed if i.kind == "close"]
    if rollback_failed:
        await pause(db, deployment, f"{problem}. Closing the legs already filled FAILED for {', '.join(rollback_failed)} "
                                    "-- open at the broker now, check it there", now)
        return LiveOutcome(action="paused", signal="ROLLBACK_FAILED", reason=problem)
    if exits_done:
        await pause(db, deployment, f"{problem}, after {', '.join(exits_done)} had closed -- part of the position is closed, "
                                    "part isn't; check it at the broker before resuming", now)
        return LiveOutcome(action="paused", signal="PARTIAL_EXIT", reason=problem)
    if await _failed_batches_today(db, deployment, now) >= MAX_FAILED_BATCHES_PER_DAY:
        await pause(db, deployment, f"{problem} -- its orders have failed {MAX_FAILED_BATCHES_PER_DAY} times today; "
                                    "paused rather than try again. Check the broker, then resume", now)
        return LiveOutcome(action="paused", signal="LEG_FAILED", reason=problem)
    if intent.kind == "close":
        await _alert(db, deployment, AlertSeverity.WARNING, AlertType.ORDER_REJECTED, "Live exit not completed",
                     f"{problem}. Nothing was closed; it still holds the position and tries again at its next check.")
    else:
        await _alert(db, deployment, AlertSeverity.WARNING, AlertType.ORDER_REJECTED, "Live entry not completed",
                     f"{problem}. The legs that filled were closed again; the strategy is flat and keeps running.")
    return LiveOutcome(action="rolled_back", signal="LEG_FAILED", reason=problem)


async def square_off(db: AsyncSession, deployment: LiveNativeDeployment, gateway, reason: str, now: datetime) -> list[str]:
    """Closes everything the deployment holds at market (buys first) and
    leaves its strategy flat; returns what couldn't be closed."""
    positions = await load_positions(db, deployment)
    intents = []
    for iid, position in positions.items():
        instrument = await db.get(Instrument, iid)
        if instrument is None:
            continue
        intents.append(Intent("close", instrument, "buy" if position.quantity < 0 else "sell", abs(position.quantity),
                              position.strategy_quantity, position.avg_price))
    failures = []
    for intent in sorted(intents, key=lambda i: 0 if i.side == "buy" else 1):
        position = positions[intent.instrument.id]
        held, entry, opened_at = position.quantity, position.avg_price, position.opened_at
        fill = await _send(db, deployment, gateway, intent, "square_off", now)
        await _apply(db, deployment, positions, intent, fill, now)
        if fill.filled_quantity > _QTY_EPS:
            await _save_trade(db, deployment, [(intent, fill, entry, held)], reason, opened_at, now)
        if not fill.complete or fill.filled_quantity + _QTY_EPS < intent.quantity:
            failures.append(f"{intent.instrument.symbol} ({fill.reason or fill.status.value})")
    deployment.state = {}
    return failures


# ------------------------------------------------------------ the check --

def _load(code: str, deployment: LiveNativeDeployment):
    namespace: dict = {}
    exec(compile(code, filename=f"<live-native-strategy:{deployment.strategy_id}>", mode="exec"), namespace)
    evaluate = namespace.get("evaluate")
    if not callable(evaluate):
        raise LiveOrderError("native strategy code must define async def evaluate(ctx)")
    return evaluate


async def _failed_batches_today(db: AsyncSession, deployment: LiveNativeDeployment, now: datetime) -> int:
    """A batch stops at its first failed order, so one failed order is one
    failed batch."""
    return int((await db.execute(
        select(func.count()).select_from(LiveOrder).where(
            LiveOrder.native_deployment_id == deployment.id, LiveOrder.created_at >= _session_start_utc(now),
            LiveOrder.purpose.in_(("open", "close")), LiveOrder.status != LiveOrderStatus.FILLED.value,
        )
    )).scalar_one())


async def _orders_today(db: AsyncSession, deployment: LiveNativeDeployment, now: datetime) -> int:
    return int((await db.execute(
        select(func.count()).select_from(LiveOrder).where(
            LiveOrder.native_deployment_id == deployment.id, LiveOrder.created_at >= _session_start_utc(now),
            LiveOrder.purpose.in_(("open", "close")),
        )
    )).scalar_one())


async def run_live_native(db: AsyncSession, deployment: LiveNativeDeployment, gateway, now: datetime | None = None,
                          force_exit: bool = False) -> LiveOutcome:
    """One check of a live strategy, its orders, and the record of both."""
    now = now or datetime.now(timezone.utc)
    deployment.last_evaluated_at = now
    outcome = await _run(db, deployment, gateway, now, force_exit)
    label = (outcome.signal or outcome.action.upper())[:20]
    deployment.last_signal = label
    deployment.last_signal_reason = (outcome.reason or "")[:500] or None
    await db.commit()
    return outcome


async def _run(db: AsyncSession, deployment: LiveNativeDeployment, gateway, now: datetime, force_exit: bool) -> LiveOutcome:
    if (await kill_switch.get_kill_switch(db)).active:
        return LiveOutcome(action="blocked", signal="KILL_SWITCH", reason="kill switch is on -- no live orders")

    pnl = await day_pnl(db, deployment, now)
    limit = loss_limit(deployment)
    if pnl <= -limit:
        failures = await square_off(db, deployment, gateway, "daily_loss_limit", now)
        message = f"Day P&L {pnl:+,.0f} reached the daily loss limit of {limit:,.0f}: squared off" + (
            f", but NOT closed: {', '.join(failures)} -- check the broker" if failures else "; resumes at the next session")
        await pause(db, deployment, message, now, resume_at=None if failures else _next_session_open(now))
        return LiveOutcome(action="paused", signal="LOSS_LIMIT", reason=message)

    account = await db.get(BrokerAccount, deployment.broker_account_id)
    if account is None or account.live_verified_at is None:
        message = "This broker account hasn't passed the broker test (Settings > Brokers) -- paused until it has"
        await pause(db, deployment, message, now)
        return LiveOutcome(action="paused", signal="NOT_TESTED", reason=message)

    version = await db.get(StrategyVersion, deployment.strategy_version_id)
    if version is None or not version.python_code:
        return LiveOutcome(action="error", reason="strategy version or code missing")
    try:
        evaluate = _load(version.python_code, deployment)
    except Exception as exc:
        return LiveOutcome(action="error", reason=f"{type(exc).__name__}: code failed to load: {exc}")

    positions = await load_positions(db, deployment)
    pool = LivePool(deployment.owner_id, await pool_cash(db, deployment, positions))
    snapshot = copy.deepcopy(deployment.state or {})
    state = copy.deepcopy(snapshot)
    if force_exit:
        state["force_exit"] = True
    ctx = LiveNativeContext(db, deployment, pool, state, positions, now)
    try:
        await evaluate(ctx)
    except LiveOrderError as exc:
        await pause(db, deployment, f"Live order problem: {exc}", now)
        return LiveOutcome(action="paused", signal="ORDER_PROBLEM", reason=str(exc))
    except Exception as exc:
        logger.exception("Live native strategy %s failed", deployment.id)
        return LiveOutcome(action="error", reason=f"{type(exc).__name__}: {exc}", wake_at=ctx._wake_at)

    if not ctx.intents:
        deployment.state = ctx.state
        return LiveOutcome(action=ctx._last_action, signal=ctx._last_signal, reason=ctx._last_reason, wake_at=ctx._wake_at)

    orders_today = await _orders_today(db, deployment, now)
    if orders_today + len(ctx.intents) > deployment.max_orders_per_day:
        message = f"{orders_today} orders today; {len(ctx.intents)} more would pass the limit of {deployment.max_orders_per_day}"
        await pause(db, deployment, message, now)
        return LiveOutcome(action="paused", signal="ORDER_LIMIT", reason=message)
    if pool.cash < 0:
        message = f"capital cap: these orders need {-pool.cash:,.0f} more than its capital of {deployment.capital:,.0f} -- not sent"
        await _alert(db, deployment, AlertSeverity.WARNING, AlertType.ORDER_REJECTED, "Live orders not sent", message)
        return LiveOutcome(action="skipped", signal="CAPITAL_CAP", reason=message)

    try:
        await record_baselines(db, deployment, gateway, ctx.intents, now)
    except LiveOrderError as exc:
        await pause(db, deployment, str(exc), now)
        return LiveOutcome(action="paused", signal="BASELINE", reason=str(exc))
    outcome = await execute_batch(db, deployment, gateway, ctx, snapshot, now)
    if outcome.action == "traded":
        outcome.action, outcome.signal = ctx._last_action, ctx._last_signal
        outcome.reason = ctx._last_reason
    outcome.wake_at = ctx._wake_at
    return outcome


# ------------------------------------------------------- your holdings --

def _key_text(key: tuple[str, str]) -> str:
    return "|".join(key)


async def _account_holders(db: AsyncSession, broker_account_id: uuid.UUID) -> list[LiveNativeDeployment]:
    return list((await db.execute(
        select(LiveNativeDeployment).where(
            LiveNativeDeployment.broker_account_id == broker_account_id,
            LiveNativeDeployment.status.in_((LIVE_NATIVE_ACTIVE, LIVE_NATIVE_PAUSED)),
        )
    )).scalars().all())


async def record_baselines(db: AsyncSession, deployment: LiveNativeDeployment, gateway, intents: list[Intent], now: datetime) -> None:
    """Before a live strategy's first trade in a contract on this account --
    none of the account's strategies holds it -- notes what the account
    already holds of it: that part is yours, and reconciliation expects it
    on top of the strategies'. Can't read the account: LiveOrderError
    (nothing is sent)."""
    opens = [i for i in intents if i.kind == "open"]
    if not opens:
        return
    held: set = set()
    for holder in await _account_holders(db, deployment.broker_account_id):
        held.update((await load_positions(db, holder)).keys())
    first = {i.instrument.id: i.instrument for i in opens if i.instrument.id not in held}
    if not first:
        return
    keys = {}
    for iid, instrument in first.items():
        try:
            keys[iid] = await gateway.key(instrument)
        except (ContractNotFound, ContractListError):
            continue  # its order won't be sent either (native_gateway)
    if not keys:
        return
    try:
        actual = await gateway.net_positions()
    except Exception as exc:
        raise LiveOrderError(f"Couldn't read what the account already holds before its first trade in "
                             f"{', '.join(first[i].symbol for i in keys)} ({exc}) -- paused, nothing sent") from exc
    for iid, key in keys.items():
        row = (await db.execute(select(LiveAccountBaseline).where(
            LiveAccountBaseline.broker_account_id == deployment.broker_account_id, LiveAccountBaseline.contract_key == _key_text(key),
        ))).scalar_one_or_none()
        if row is None:
            row = LiveAccountBaseline(broker_account_id=deployment.broker_account_id, contract_key=_key_text(key))
            db.add(row)
        row.symbol, row.quantity, row.recorded_at = first[iid].symbol[:50], float(actual.get(key, 0.0)), now
    await db.flush()


# ---------------------------------------------------------- account-wide --

async def account_limit(db: AsyncSession, user_id: uuid.UUID, deployments: list[LiveNativeDeployment]) -> float:
    settings = await db.get(LiveRiskSettings, user_id)
    if settings is not None and settings.account_daily_loss_limit is not None:
        return settings.account_daily_loss_limit
    return sum(d.capital for d in deployments) * DEFAULT_ACCOUNT_LOSS_PCT / 100


async def check_account_limits(db: AsyncSession, gateway_for, now: datetime) -> list[uuid.UUID]:
    """Squares off every live strategy of a user whose combined day P&L is
    past their account-wide limit, pauses them, and turns the kill switch
    on. Returns the users it did this for."""
    rows = (await db.execute(
        select(LiveNativeDeployment).where(LiveNativeDeployment.status.in_((LIVE_NATIVE_ACTIVE, LIVE_NATIVE_PAUSED)))
    )).scalars().all()
    by_user: dict[uuid.UUID, list[LiveNativeDeployment]] = defaultdict(list)
    for deployment in rows:
        by_user[deployment.owner_id].append(deployment)
    hit = []
    for user_id, deployments in by_user.items():
        if not any(d.status == LIVE_NATIVE_ACTIVE for d in deployments):
            continue  # all already paused: done (and alerted) at an earlier check
        total = sum([await day_pnl(db, d, now) for d in deployments])
        limit = await account_limit(db, user_id, deployments)
        if limit <= 0 or total > -limit:
            continue
        reason = f"All live strategies together: day P&L {total:+,.0f} reached the account limit of {limit:,.0f}"
        for deployment in deployments:
            failures = await square_off(db, deployment, await gateway_for(deployment), "account_loss_limit", now)
            note = reason + (f" -- NOT closed: {', '.join(failures)}" if failures else " -- squared off")
            await pause(db, deployment, note, now)
        await kill_switch.activate(db, user_id, reason[:200])
        await _alert(db, deployments[0], AlertSeverity.CRITICAL, AlertType.KILL_SWITCH_ACTIVATED, "Kill switch ON", reason)
        hit.append(user_id)
    await db.commit()
    return hit


# --------------------------------------------------------- reconciliation --

async def reconcile_account(db: AsyncSession, broker_account_id: uuid.UUID, gateway, now: datetime) -> list[str]:
    """Pauses every live strategy on this broker account holding an
    instrument whose quantity the broker reports differently; returns the
    differences found."""
    deployments = (await db.execute(
        select(LiveNativeDeployment).where(
            LiveNativeDeployment.broker_account_id == broker_account_id,
            LiveNativeDeployment.status.in_((LIVE_NATIVE_ACTIVE, LIVE_NATIVE_PAUSED)),  # a paused one's holdings are at the broker too
        )
    )).scalars().all()
    if not any(d.status == LIVE_NATIVE_ACTIVE for d in deployments):
        return []
    expected: dict[tuple[str, str], float] = defaultdict(float)
    holders: dict[tuple[str, str], list[LiveNativeDeployment]] = defaultdict(list)
    names: dict[tuple[str, str], str] = {}
    unmatched: list[tuple[str, LiveNativeDeployment]] = []
    for deployment in deployments:
        for position in (await load_positions(db, deployment)).values():
            instrument = await db.get(Instrument, position.instrument_id)
            if instrument is None:
                continue
            try:
                key = await gateway.key(instrument)
            except (ContractNotFound, ContractListError) as exc:
                unmatched.append((f"{instrument.symbol}: can't be checked at the broker ({exc})", deployment))
                continue
            expected[key] += position.quantity
            holders[key].append(deployment)
            names[key] = instrument.symbol
    try:
        actual = await gateway.net_positions()
    except Exception as exc:
        reason = f"Couldn't read positions from the broker ({exc}) -- paused until it can be checked"
        for deployment in deployments:
            if deployment.status == LIVE_NATIVE_ACTIVE and any(deployment in holders[k] for k in holders):
                await pause(db, deployment, reason[:500], now)
        await db.commit()
        return [reason]
    yours = {row.contract_key: row.quantity for row in (await db.execute(
        select(LiveAccountBaseline).where(LiveAccountBaseline.broker_account_id == broker_account_id)
    )).scalars()}
    differences = []
    for text, deployment in unmatched:
        differences.append(text)
        if deployment.status == LIVE_NATIVE_ACTIVE:
            await pause(db, deployment, f"{text}. Check it there, then resume.", now)
    for key, quantity in expected.items():
        broker_quantity = actual.get(key, 0.0)
        own = yours.get(_key_text(key), 0.0)
        if abs(broker_quantity - quantity - own) > _QTY_EPS:
            text = f"{names[key]}: the broker holds {broker_quantity:g}, the live strategies {quantity:g}" + (
                f" and you {own:g} of your own -- if you traded it yourself, that's the difference" if own else "")
            differences.append(text)
            for deployment in holders[key]:
                if deployment.status == LIVE_NATIVE_ACTIVE:
                    await pause(db, deployment, f"Doesn't match the broker -- {text}. Check it there, then resume.", now)
    await db.commit()
    return differences


__all__ = [
    "LiveNativeContext", "LiveOrderError", "LiveOutcome", "LivePool", "account_limit", "check_account_limits", "day_pnl",
    "execute_batch", "load_positions", "loss_limit", "pause", "pool_cash", "reconcile_account", "record_baselines", "run_live_native",
    "square_off",
]
