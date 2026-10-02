"""The Trading page's Paper / Live switch (agreed 2 Oct 2026).

A native strategy's paper card can be switched to Live on one of its owner's
broker accounts. That starts a live run (LiveNativeDeployment, linked to the
paper run by paper_deployment_id) which native_scheduler runs through
native_live; the paper run keeps going beside it at its own size.

  - Start: nothing is bought at the switch. The live run starts flat (an
    empty state) and does what the strategy does when freshly started
    (NIFTY PCR Strategy v2: waits for the next 15-minute close). Size is
    typed in: lots per leg (F&O) and the capital it may use, plus the daily
    loss limit and the product (overnight NRML/CNC or intraday MIS).
  - Only an administrator or trader, on their own live broker account,
    of a broker live strategies trade through, that passed the broker test.
  - Edit: lots, capital and the loss limit any time (a change applies from
    the next entry; an open position closes at the size it has); the
    product only while it holds nothing.
  - Turning it off with positions open asks each time: close them now
    (protected limit orders, as every live order is) or leave them in the
    broker account. Left ones become "yours" for reconciliation wherever
    another live strategy on that account holds the same contract, so it
    isn't paused for a difference you chose.
  - Exit all: every live position of the user closed, every live run
    paused until resumed.

Everything that changes a live run takes native_scheduler's lock, so it never
interleaves with that run's check.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.alert import AlertSeverity
from app.models.broker import Broker, BrokerAccount, BrokerConnection, Environment
from app.models.instrument import Instrument
from app.models.live_native import (
    LIVE_NATIVE_ACTIVE,
    LIVE_NATIVE_PAUSED,
    LIVE_NATIVE_STOPPED,
    PRODUCT_INTRADAY,
    PRODUCT_OVERNIGHT,
    LiveAccountBaseline,
    LiveNativeDeployment,
)
from app.models.paper_trading import PaperNativeDeployment
from app.services.backfill_platform.coverage import IST
from app.services.broker.registry import supports_live_strategies
from app.services.live_trading import kill_switch
from app.services.live_trading.broker_contracts import ContractListError, ContractNotFound
from app.services.live_trading.native_live import (
    _account_holders,
    _key_text,
    _mark,
    _orders_today,
    day_pnl,
    load_positions,
    loss_limit,
    pause,
    square_off,
)
from app.services.market_data.hours import nse_market_open

ACTIVE_STATES = (LIVE_NATIVE_ACTIVE, LIVE_NATIVE_PAUSED)
PRODUCTS = (PRODUCT_OVERNIGHT, PRODUCT_INTRADAY)
# NIFTY's exchange freeze quantity is 1,800 (27 lots of 65): an order above an
# underlying's freeze limit is rejected unless split, which the gateway doesn't do.
MAX_LOTS_PER_LEG = 25

# What fixes a paused live run (the card's fix-it button).
FIX_LOGIN = "login"
FIX_TEST = "test"
FIX_LOSS_LIMIT = "loss_limit"
FIX_KILL_SWITCH = "kill_switch"
FIX_RESUME = "resume"
FIX_CHECK_BROKER = "check_broker"


class LiveRunError(Exception):
    """Something the person asked for that can't be done, and why."""


@dataclass
class Size:
    lots_per_leg: int
    capital: float
    daily_loss_limit: float | None
    product_style: str


def check_size(size: Size) -> None:
    if not 1 <= size.lots_per_leg <= MAX_LOTS_PER_LEG:
        raise LiveRunError(f"Lots per leg must be 1 to {MAX_LOTS_PER_LEG} -- a bigger order has to be split, which the app doesn't do yet")
    if size.capital <= 0:
        raise LiveRunError("Type the capital this strategy may use live")
    if size.daily_loss_limit is not None and size.daily_loss_limit <= 0:
        raise LiveRunError("The daily loss limit must be more than zero (or left empty for 3% of capital)")
    if size.product_style not in PRODUCTS:
        raise LiveRunError("Product must be overnight or intraday")


async def live_account(db: AsyncSession, user_id: uuid.UUID, broker_account_id: uuid.UUID) -> tuple[BrokerAccount, Broker]:
    """The user's own broker account, if live strategies may trade on it."""
    account = await db.get(BrokerAccount, broker_account_id)
    if account is None or account.user_id != user_id:
        raise LiveRunError("Broker account not found")
    broker = await db.get(Broker, account.broker_id)
    if account.environment != Environment.LIVE.value or not account.is_active:
        raise LiveRunError(f"{account.account_label} isn't an active live account")
    if not supports_live_strategies(broker.code):
        raise LiveRunError(f"Live strategies can't trade through {broker.name} yet")
    if account.live_verified_at is None:
        raise LiveRunError(f"{broker.name} ({account.account_label}) hasn't passed the broker test -- run Test in Settings > Brokers first")
    return account, broker


async def linked_run(db: AsyncSession, paper_deployment_id: uuid.UUID) -> LiveNativeDeployment | None:
    """The live run switched on from this paper card, while it's on."""
    return (await db.execute(
        select(LiveNativeDeployment).where(
            LiveNativeDeployment.paper_deployment_id == paper_deployment_id, LiveNativeDeployment.status.in_(ACTIVE_STATES),
        ).order_by(LiveNativeDeployment.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def start_live_run(db: AsyncSession, owner_id: uuid.UUID, paper: PaperNativeDeployment, account: BrokerAccount,
                         size: Size) -> LiveNativeDeployment:
    check_size(size)
    if (await kill_switch.get_kill_switch(db)).active:
        raise LiveRunError("The kill switch is on -- no strategy can go live until an administrator turns it off")
    if await linked_run(db, paper.id) is not None:
        raise LiveRunError("This strategy is already live")
    run = LiveNativeDeployment(
        owner_id=owner_id, strategy_id=paper.strategy_id, strategy_version_id=paper.strategy_version_id,
        broker_account_id=account.id, paper_deployment_id=paper.id, status=LIVE_NATIVE_ACTIVE, state=None,
        lots_per_leg=size.lots_per_leg, capital=size.capital, daily_loss_limit=size.daily_loss_limit,
        product_style=size.product_style,
    )
    db.add(run)
    await db.flush()
    return run


async def edit_live_run(db: AsyncSession, run: LiveNativeDeployment, size: Size) -> None:
    check_size(size)
    if size.product_style != run.product_style and await load_positions(db, run):
        raise LiveRunError("The product can only change while it holds nothing -- its open positions close as they were opened")
    run.lots_per_leg, run.capital, run.daily_loss_limit, run.product_style = (
        size.lots_per_leg, size.capital, size.daily_loss_limit, size.product_style)


async def resume_live_run(db: AsyncSession, run: LiveNativeDeployment) -> None:
    if run.status != LIVE_NATIVE_PAUSED:
        raise LiveRunError("It isn't paused")
    if (await kill_switch.get_kill_switch(db)).active:
        raise LiveRunError("The kill switch is on -- an administrator has to turn it off first")
    account = await db.get(BrokerAccount, run.broker_account_id)
    if account is None or account.live_verified_at is None:
        raise LiveRunError("Its broker account hasn't passed the broker test -- run Test in Settings > Brokers first")
    run.status = LIVE_NATIVE_ACTIVE
    run.pause_reason = run.paused_at = run.resume_at = None


async def _keep_as_yours(db: AsyncSession, run: LiveNativeDeployment, open_gateway, now: datetime) -> list[str]:
    """Positions left in the broker account when live goes off: the run
    stops holding them. Where another live strategy on the account holds the
    same contract, the quantity is added to your own (LiveAccountBaseline)
    so reconciliation still adds up; elsewhere nothing needs noting -- the
    next strategy to trade it records what the account holds first."""
    positions = await load_positions(db, run)
    if not positions:
        return []
    shared: set = set()
    for other in await _account_holders(db, run.broker_account_id):
        if other.id != run.id:
            shared.update((await load_positions(db, other)).keys())
    overlap = [p for iid, p in positions.items() if iid in shared]
    if overlap:
        try:
            gateway = await open_gateway(db, run.broker_account_id)
        except Exception as exc:
            raise LiveRunError(f"Couldn't reach the broker to note the positions you're keeping ({exc}) -- log in and try again") from exc
        for position in overlap:
            instrument = await db.get(Instrument, position.instrument_id)
            try:
                key = _key_text(await gateway.key(instrument))
            except (ContractNotFound, ContractListError) as exc:
                raise LiveRunError(f"{instrument.symbol}: couldn't match it at the broker ({exc}) -- try again later") from exc
            row = (await db.execute(select(LiveAccountBaseline).where(
                LiveAccountBaseline.broker_account_id == run.broker_account_id, LiveAccountBaseline.contract_key == key,
            ))).scalar_one_or_none()
            if row is None:
                row = LiveAccountBaseline(broker_account_id=run.broker_account_id, contract_key=key, quantity=0.0)
                db.add(row)
            row.symbol, row.quantity, row.recorded_at = instrument.symbol[:50], (row.quantity or 0.0) + position.quantity, now
    left = []
    for position in positions.values():
        instrument = await db.get(Instrument, position.instrument_id)
        left.append(f"{position.quantity:+g} {instrument.symbol if instrument else position.instrument_id}")
        await db.delete(position)
    await db.flush()
    return left


async def stop_live_run(db: AsyncSession, run: LiveNativeDeployment, close_positions: bool, open_gateway, now: datetime) -> list[str]:
    """Turns live off. Returns what was left in the broker account (leave),
    or nothing. Closing what couldn't be closed: the run is paused, not
    stopped, and LiveRunError says what's still held."""
    if run.status not in ACTIVE_STATES:
        raise LiveRunError("Live is already off")
    held = await load_positions(db, run)
    left: list[str] = []
    if held and close_positions:
        if not nse_market_open(now):
            raise LiveRunError("NSE is closed -- positions can be closed from 09:15, or choose to leave them in your broker account")
        try:
            gateway = await open_gateway(db, run.broker_account_id)
        except Exception as exc:
            raise LiveRunError(f"Couldn't log in to the broker ({exc}) -- nothing was closed") from exc
        failures = await square_off(db, run, gateway, "switched_off", now)
        if failures:
            await pause(db, run, f"Turning live off: couldn't close {', '.join(failures)} -- still held; close it at the broker "
                                 "or try again", now)
            await db.flush()
            raise LiveRunError(f"Couldn't close {', '.join(failures)}; it's paused with that still held")
    elif held:
        left = await _keep_as_yours(db, run, open_gateway, now)
    run.status = LIVE_NATIVE_STOPPED
    run.stopped_at = now
    run.pause_reason = run.resume_at = None
    return left


async def exit_all(db: AsyncSession, owner_id: uuid.UUID, open_gateway, now: datetime) -> tuple[int, list[str]]:
    """Every live position of the user closed and every live run paused.
    Returns (runs paused, what couldn't be closed)."""
    runs = list((await db.execute(
        select(LiveNativeDeployment).where(LiveNativeDeployment.owner_id == owner_id, LiveNativeDeployment.status.in_(ACTIVE_STATES))
    )).scalars().all())
    holding = [run for run in runs if await load_positions(db, run)]
    if holding and not nse_market_open(now):
        raise LiveRunError("NSE is closed -- live positions can be closed from 09:15")
    failures: list[str] = []
    gateways: dict = {}
    for run in runs:
        problems: list[str] = []
        if run in holding:
            try:
                if run.broker_account_id not in gateways:
                    gateways[run.broker_account_id] = await open_gateway(db, run.broker_account_id)
                problems = await square_off(db, run, gateways[run.broker_account_id], "exit_all", now)
            except Exception as exc:
                problems = [f"everything ({exc})"]
        failures += problems
        stamp = now.astimezone(IST).strftime("%H:%M")
        reason = (f"Exit all at {stamp}: couldn't close {', '.join(problems)} -- check the broker" if problems
                  else f"Exit all at {stamp}: you closed every live position -- resume when ready")
        await pause(db, run, reason, now, severity=AlertSeverity.CRITICAL if problems else AlertSeverity.INFO)
    await db.flush()
    return len(runs), failures


def pause_fix(run: LiveNativeDeployment, kill_switch_on: bool) -> str | None:
    """Which fix-it button a paused (or blocked) run's card offers."""
    if kill_switch_on:
        return FIX_KILL_SWITCH
    if run.status != LIVE_NATIVE_PAUSED:
        return None
    reason = (run.pause_reason or "").lower()
    if "couldn't log in" in reason:
        return FIX_LOGIN
    if "broker test" in reason:
        return FIX_TEST
    if "daily loss limit" in reason:
        return FIX_LOSS_LIMIT
    if "exit all" in reason and "couldn't close" not in reason:
        return FIX_RESUME
    return FIX_CHECK_BROKER


async def run_view(db: AsyncSession, run: LiveNativeDeployment, now: datetime, kill_switch_on: bool) -> dict:
    """What the card shows for a live run: its positions at live prices,
    today's P&L against its loss limit, orders today, and the fix."""
    account = await db.get(BrokerAccount, run.broker_account_id)
    broker = await db.get(Broker, account.broker_id) if account else None
    connection = (await db.execute(
        select(BrokerConnection).where(BrokerConnection.broker_account_id == run.broker_account_id)
    )).scalar_one_or_none()
    legs, unrealised = [], 0.0
    for position in (await load_positions(db, run)).values():
        instrument = await db.get(Instrument, position.instrument_id)
        if instrument is None:
            continue
        price = await _mark(db, instrument, now)
        pnl = (price - position.avg_price) * position.quantity if price is not None else None
        unrealised += pnl or 0.0
        lots = abs(position.quantity) / instrument.lot_size if instrument.lot_size and instrument.instrument_type in ("option", "future") else None
        legs.append({
            "instrument_symbol": instrument.symbol, "instrument_type": instrument.instrument_type, "strike": instrument.strike,
            "option_type": instrument.option_type, "expiry": instrument.expiry, "side": "long" if position.quantity > 0 else "short",
            "quantity": abs(position.quantity), "lots": lots, "avg_price": position.avg_price, "current_price": price, "pnl": pnl,
            "product": position.product, "opened_at": _aware(position.opened_at),
        })
    today = await day_pnl(db, run, now)
    state = run.state or {}
    return {
        "id": str(run.id), "paper_deployment_id": str(run.paper_deployment_id) if run.paper_deployment_id else None,
        "strategy_id": str(run.strategy_id), "status": run.status,
        "broker_account_id": str(run.broker_account_id), "broker_code": broker.code if broker else "",
        "broker_name": broker.name if broker else "", "account_label": account.account_label if account else "",
        "connection_status": connection.status if connection else "disconnected",
        "lots_per_leg": run.lots_per_leg, "capital": run.capital, "daily_loss_limit": run.daily_loss_limit,
        "loss_limit": loss_limit(run), "product_style": run.product_style, "max_orders_per_day": run.max_orders_per_day,
        "day_pnl": today, "unrealised_pnl": unrealised, "realised_today": today - unrealised,
        "orders_today": await _orders_today(db, run, now), "positions": legs,
        "pause_reason": run.pause_reason, "paused_at": _aware(run.paused_at), "resume_at": _aware(run.resume_at),
        "fix": pause_fix(run, kill_switch_on),
        "last_evaluated_at": _aware(run.last_evaluated_at), "last_signal": run.last_signal, "last_signal_reason": run.last_signal_reason,
        "state": {k: v for k, v in state.items() if not k.startswith("_")}, "created_at": _aware(run.created_at),
    }


def _aware(value: datetime | None) -> datetime | None:
    return as_aware_utc(value) if value is not None else None
