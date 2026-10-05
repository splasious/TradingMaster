"""Live native strategies -- what Settings > Brokers needs before any can run:
the broker test (live_trading/broker_test.py) and the server's public IP,
which every broker must have registered as the static IP API orders come
from (SEBI's retail algo rules, since 1 April 2026) -- and the Trading page's
Paper / Live switch (live_trading/live_runs.py): a live run started from a
paper card, edited, resumed, turned off, every live position exited, and
the live trades."""

import time
import uuid
from datetime import date, datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.db.session import get_db
from app.core.time import as_aware_utc
from app.models.broker import Broker, BrokerAccount, Environment
from app.models.instrument import Instrument
from app.models.live_native import LiveNativeDeployment, LiveNativeTrade
from app.models.paper_trading import PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy
from app.models.user import User
from app.schemas.paper_trading import ClosePriceOut, NativeTradeOut
from app.services.audit import write_audit_log
from app.services.broker.registry import supports_live_strategies
from app.services.live_trading import kill_switch
from app.services.live_trading.broker_test import MAX_TEST_PRICE, can_test_now, run_broker_test
from app.services.live_trading.live_runs import (
    ACTIVE_STATES,
    LiveRunError,
    Size,
    edit_live_run,
    exit_all,
    live_account,
    resume_live_run,
    run_view,
    start_live_run,
    stop_live_run,
)
from app.services.live_trading.native_scheduler import _live_lock, open_gateway
from app.services.market_data.live_price import live_price
from app.services.paper_trading.trade_record import estimate_charges, resolve_leg_details, summarize_trade

router = APIRouter()


class BrokerTestIn(BaseModel):
    broker_account_id: uuid.UUID
    symbol: str


class BrokerTestStepOut(BaseModel):
    name: str
    ok: bool
    detail: str


class BrokerTestOut(BaseModel):
    passed: bool
    steps: list[BrokerTestStepOut]
    contracts: list[dict]
    still_held: str | None = None
    live_verified_at: datetime | None = None


class ServerIpOut(BaseModel):
    ipv4: str | None
    ipv6: str | None


@router.post("/broker-test", response_model=BrokerTestOut)
async def broker_test(
    payload: BrokerTestIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader")),
) -> BrokerTestOut:
    account = await db.get(BrokerAccount, payload.broker_account_id)
    if account is None or (account.user_id != user.id and "administrator" not in user.role_names):
        raise HTTPException(status_code=404, detail="Broker account not found")
    broker = await db.get(Broker, account.broker_id)
    if account.environment != Environment.LIVE.value:
        raise HTTPException(status_code=400, detail="Only a live broker account can be tested")
    if not supports_live_strategies(broker.code):
        raise HTTPException(status_code=400, detail=f"Live strategies can't trade through {broker.name} yet")
    now = datetime.now(timezone.utc)
    why_not = can_test_now(now)
    if why_not:
        raise HTTPException(status_code=400, detail=why_not)
    if (await kill_switch.get_kill_switch(db)).active:
        raise HTTPException(status_code=400, detail="The kill switch is on -- no live orders")

    symbol = payload.symbol.strip().upper()
    stock = (await db.execute(
        select(Instrument).where(
            Instrument.exchange == "NSE", Instrument.instrument_type == "equity", func.upper(Instrument.symbol) == symbol,
            Instrument.is_active.is_(True),
        )
    )).scalars().first()
    if stock is None:
        raise HTTPException(status_code=400, detail=f"{symbol} isn't in this app's NSE stock list -- pick another")
    price = await live_price(db, stock, now)
    if not price:
        raise HTTPException(status_code=400, detail=f"No live price for {symbol} right now")
    if price > MAX_TEST_PRICE:
        raise HTTPException(status_code=400, detail=f"{symbol} is at {price:,.2f}; pick a stock under {MAX_TEST_PRICE:,.0f} for the test")

    try:
        gateway = await open_gateway(db, account.id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Couldn't log in to {broker.name}: {exc}") from exc
    report = await run_broker_test(db, account, user.id, stock, gateway, now)
    await write_audit_log(
        db, user_id=user.id, action="BROKER_TEST", object_type="broker_account", object_id=str(account.id),
        new_value={"passed": report.passed, "stock": stock.symbol, "failed_steps": [s.name for s in report.steps if not s.ok]},
    )
    await db.commit()
    return BrokerTestOut(
        passed=report.passed, steps=[BrokerTestStepOut(**vars(s)) for s in report.steps], contracts=report.contracts,
        still_held=report.still_held, live_verified_at=account.live_verified_at,
    )


_IP_TTL_SECONDS = 600
_ip_cache: tuple[float, ServerIpOut] | None = None


async def _ask(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        return response.text.strip() or None if response.status_code == 200 else None
    except httpx.HTTPError:
        return None


@router.get("/server-ip", response_model=ServerIpOut)
async def server_ip(_: User = Depends(get_current_user)) -> ServerIpOut:
    """The public IP(s) this server's requests reach the internet from --
    what to register with each broker. api64 answers over IPv6 when the
    server has it: a broker reached over IPv6 sees that address, not the
    IPv4 one."""
    global _ip_cache
    if _ip_cache is not None and time.monotonic() - _ip_cache[0] < _IP_TTL_SECONDS:
        return _ip_cache[1]
    ipv4 = await _ask("https://api.ipify.org")
    other = await _ask("https://api64.ipify.org")
    out = ServerIpOut(ipv4=ipv4, ipv6=other if other and ":" in other else None)
    if ipv4 or out.ipv6:
        _ip_cache = (time.monotonic(), out)
    return out


# ------------------------------------------------- the Paper / Live switch --

class LiveRunIn(BaseModel):
    paper_deployment_id: uuid.UUID
    broker_account_id: uuid.UUID
    lots_per_leg: int
    capital: float
    daily_loss_limit: float | None = None
    product_style: str = "overnight"
    confirmed: bool = False


class LiveRunEdit(BaseModel):
    lots_per_leg: int
    capital: float
    daily_loss_limit: float | None = None
    product_style: str


class LiveRunStopIn(BaseModel):
    close_positions: bool


class LiveLegOut(BaseModel):
    instrument_symbol: str
    instrument_type: str
    strike: float | None = None
    option_type: str | None = None
    expiry: date | None = None
    side: str
    quantity: float
    lots: float | None = None
    avg_price: float
    current_price: float | None = None
    pnl: float | None = None
    product: str | None = None
    opened_at: datetime | None = None
    close: ClosePriceOut | None = None  # set while NSE is shut: current_price is the session's close


class LiveRunOut(BaseModel):
    id: str
    paper_deployment_id: str | None
    strategy_id: str
    status: str
    broker_account_id: str
    broker_code: str
    broker_name: str
    account_label: str
    connection_status: str
    lots_per_leg: int
    capital: float
    daily_loss_limit: float | None
    loss_limit: float
    product_style: str
    max_orders_per_day: int
    day_pnl: float
    unrealised_pnl: float
    realised_today: float
    orders_today: int
    positions: list[LiveLegOut]
    pause_reason: str | None
    paused_at: datetime | None
    resume_at: datetime | None
    fix: str | None
    last_evaluated_at: datetime | None
    last_signal: str | None
    last_signal_reason: str | None
    state: dict
    created_at: datetime


class LiveRunStopOut(BaseModel):
    left_in_account: list[str]


class ExitAllOut(BaseModel):
    paused: int
    not_closed: list[str]


class LiveTradeOut(NativeTradeOut):
    paper_deployment_id: str | None = None
    broker_name: str
    account_label: str


async def _view(db: AsyncSession, run: LiveNativeDeployment, now: datetime) -> LiveRunOut:
    on = (await kill_switch.get_kill_switch(db)).active
    return LiveRunOut(**await run_view(db, run, now, on))


async def _own_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> LiveNativeDeployment:
    run = await db.get(LiveNativeDeployment, run_id)
    if run is None or run.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Live run not found")
    return run


def _size(payload: LiveRunIn | LiveRunEdit) -> Size:
    return Size(lots_per_leg=payload.lots_per_leg, capital=payload.capital, daily_loss_limit=payload.daily_loss_limit,
                product_style=payload.product_style)


@router.get("/runs", response_model=list[LiveRunOut])
async def list_live_runs(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[LiveRunOut]:
    """The caller's live runs that are on (running or paused)."""
    runs = (await db.execute(
        select(LiveNativeDeployment).where(LiveNativeDeployment.owner_id == user.id, LiveNativeDeployment.status.in_(ACTIVE_STATES))
        .order_by(LiveNativeDeployment.created_at)
    )).scalars().all()
    now = datetime.now(timezone.utc)
    out = [await _view(db, run, now) for run in runs]
    await db.commit()
    return out


@router.post("/runs", response_model=LiveRunOut, status_code=201)
async def start_live(
    payload: LiveRunIn, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader")),
) -> LiveRunOut:
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Confirm that this trades real money first")
    paper = await db.get(PaperNativeDeployment, payload.paper_deployment_id)
    portfolio = await db.get(PaperPortfolio, paper.portfolio_id) if paper else None
    if paper is None or portfolio is None or portfolio.user_id != user.id:
        raise HTTPException(status_code=404, detail="Strategy deployment not found")
    try:
        async with _live_lock:
            account, broker = await live_account(db, user.id, payload.broker_account_id)
            run = await start_live_run(db, user.id, paper, account, _size(payload))
    except LiveRunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await write_audit_log(
        db, user_id=user.id, action="LIVE_NATIVE_STARTED", object_type="live_native_deployment", object_id=str(run.id),
        new_value={"paper_deployment_id": str(paper.id), "broker": broker.code, "account": account.account_label,
                   "lots_per_leg": run.lots_per_leg, "capital": run.capital, "daily_loss_limit": run.daily_loss_limit,
                   "product_style": run.product_style},
    )
    await db.commit()
    await db.refresh(run)
    return await _view(db, run, datetime.now(timezone.utc))


@router.patch("/runs/{run_id}", response_model=LiveRunOut)
async def edit_live(
    run_id: uuid.UUID, payload: LiveRunEdit, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader")),
) -> LiveRunOut:
    run = await _own_run(db, user, run_id)
    before = {"lots_per_leg": run.lots_per_leg, "capital": run.capital, "daily_loss_limit": run.daily_loss_limit,
              "product_style": run.product_style}
    try:
        async with _live_lock:
            await db.refresh(run)
            await edit_live_run(db, run, _size(payload))
    except LiveRunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await write_audit_log(
        db, user_id=user.id, action="LIVE_NATIVE_EDITED", object_type="live_native_deployment", object_id=str(run.id),
        previous_value=before, new_value={"lots_per_leg": run.lots_per_leg, "capital": run.capital,
                                          "daily_loss_limit": run.daily_loss_limit, "product_style": run.product_style},
    )
    await db.commit()
    return await _view(db, run, datetime.now(timezone.utc))


@router.post("/runs/{run_id}/resume", response_model=LiveRunOut)
async def resume_live(
    run_id: uuid.UUID, db: AsyncSession = Depends(get_db), user: User = Depends(require_role("administrator", "trader")),
) -> LiveRunOut:
    run = await _own_run(db, user, run_id)
    reason = run.pause_reason
    try:
        async with _live_lock:
            await db.refresh(run)
            await resume_live_run(db, run)
    except LiveRunError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await write_audit_log(
        db, user_id=user.id, action="LIVE_NATIVE_RESUMED", object_type="live_native_deployment", object_id=str(run.id),
        previous_value={"pause_reason": reason},
    )
    await db.commit()
    return await _view(db, run, datetime.now(timezone.utc))


@router.post("/runs/{run_id}/stop", response_model=LiveRunStopOut)
async def stop_live(
    run_id: uuid.UUID, payload: LiveRunStopIn, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> LiveRunStopOut:
    """Turns live off -- any logged-in owner may, whatever their role now:
    stopping real money is never refused for lack of a role."""
    run = await _own_run(db, user, run_id)
    now = datetime.now(timezone.utc)
    try:
        async with _live_lock:
            await db.refresh(run)
            left = await stop_live_run(db, run, payload.close_positions, open_gateway, now)
    except LiveRunError as exc:
        await db.commit()  # a failed close leaves it paused, and that is kept
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await write_audit_log(
        db, user_id=user.id, action="LIVE_NATIVE_STOPPED", object_type="live_native_deployment", object_id=str(run.id),
        new_value={"closed_positions": payload.close_positions, "left_in_account": left},
    )
    await db.commit()
    return LiveRunStopOut(left_in_account=left)


@router.post("/exit-all", response_model=ExitAllOut)
async def exit_all_live(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> ExitAllOut:
    now = datetime.now(timezone.utc)
    try:
        async with _live_lock:
            paused, not_closed = await exit_all(db, user.id, open_gateway, now)
    except LiveRunError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await write_audit_log(db, user_id=user.id, action="LIVE_NATIVE_EXIT_ALL", new_value={"paused": paused, "not_closed": not_closed})
    await db.commit()
    return ExitAllOut(paused=paused, not_closed=not_closed)


@router.get("/trades", response_model=list[LiveTradeOut])
async def list_live_trades(
    paper_deployment_id: uuid.UUID | None = None, limit: int = 200,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> list[LiveTradeOut]:
    """The caller's closed live trades, newest first -- at the broker's
    fills, the way native_live recorded them."""
    stmt = (
        select(LiveNativeTrade, LiveNativeDeployment, Strategy.name, Broker.name, BrokerAccount.account_label)
        .join(LiveNativeDeployment, LiveNativeTrade.deployment_id == LiveNativeDeployment.id)
        .join(Strategy, LiveNativeDeployment.strategy_id == Strategy.id)
        .join(BrokerAccount, LiveNativeDeployment.broker_account_id == BrokerAccount.id)
        .join(Broker, BrokerAccount.broker_id == Broker.id)
        .where(LiveNativeDeployment.owner_id == user.id)
        .order_by(LiveNativeTrade.closed_at.desc())
        .limit(max(1, min(limit, 1000)))
    )
    if paper_deployment_id is not None:
        stmt = stmt.where(LiveNativeDeployment.paper_deployment_id == paper_deployment_id)
    rows = (await db.execute(stmt)).all()
    resolved = await resolve_leg_details(db, [t.legs or [] for t, *_ in rows])
    out = []
    for (t, run, name, broker_name, label), legs in zip(rows, resolved):
        charges = t.charges if t.charges is not None else estimate_charges(legs, t.opened_at, t.closed_at)
        out.append(LiveTradeOut(
            id=str(t.id), deployment_id=str(t.deployment_id), strategy_name=name, currency="INR",
            opened_at=as_aware_utc(t.opened_at), closed_at=as_aware_utc(t.closed_at), **summarize_trade(legs),
            pnl=t.pnl, charges=charges, net_pnl=t.pnl - (charges or 0.0), pnl_pct=t.pnl_pct, exit_reason=t.exit_reason,
            paper_deployment_id=str(run.paper_deployment_id) if run.paper_deployment_id else None,
            broker_name=broker_name, account_label=label,
        ))
    return out
