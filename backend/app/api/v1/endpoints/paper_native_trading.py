import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_role
from app.core.time import as_aware_utc
from app.db.session import get_db
from app.models.instrument import Instrument
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.models.user import User
from app.schemas.paper_trading import (
    NativeDeploymentCreate,
    NativeDeploymentOrderIn,
    NativeDeploymentOut,
    NativeEvaluationOut,
    NativeHoldingOut,
    NativeLegOut,
    NativePositionOut,
    NativeTradeOut,
)
from app.services.audit import write_audit_log
from app.services.market_data.tick_engine import tick_engine
from app.services.live_trading.live_runs import linked_run
from app.services.paper_trading.native_runner import exit_native_deployment_now, run_native_strategy
from app.services.paper_trading.scheduler import paper_trading_scheduler
from app.services.paper_trading.trade_record import estimate_charges, resolve_leg_details, summarize_trade
from app.services.strategy.state_machine import StrategyStatus, can_transition

router = APIRouter()


async def _build_position_out(db: AsyncSession, state: dict | None) -> NativePositionOut | None:
    """Display-only reconstruction of a deployment's open position from its
    raw state blob -- returns None for any state that isn't shaped like
    one of the two real conventions that exist today (flat, or a future
    native strategy with some other convention) rather than guessing.

    Two shapes exist: nifty_pcr_credit_spread.py's fixed 2-leg
    {"short": {...}, "long": {...}} (side implied by the dict key), and
    nifty_pcr_multi_regime's variable {"legs": {name: {"side": "sell" |
    "buy", ...}, ...}} (2 legs for a directional spread, 4 for the
    sideways iron condor). Both get normalized into the same (side, leg)
    list below so the rest of this function -- and the Trade Value/Live
    Value/Unrealized P&L math -- doesn't care which strategy produced the
    state. Without this, a multi-regime deployment's dashboard card always
    showed "flat" with a real position open underneath it, since this
    function used to recognize only the first shape."""
    position = (state or {}).get("position") if state else None
    if not isinstance(position, dict) and isinstance((state or {}).get("legs"), dict) and state["legs"]:
        # The third shape: legs and regime at the top of the state (NIFTY PCR
        # Strategy -- {"regime": ..., "legs": {"future": {...}, "option": {...}}}).
        opened = min((leg["opened_at"] for leg in state["legs"].values() if leg.get("opened_at")), default=None)
        if opened is None:
            return None
        # Its PCR is its own TOTAL_OI_PCR, not the 4-expiry one the card's
        # "PCR (entry -> now)" compares with, so it's shown as text under
        # names of its own.
        position = {"legs": state["legs"], "regime": state.get("regime"), "opened_at": opened}
        if isinstance(state.get("pcr_at_entry"), (int, float)):
            position["total_oi_pcr_at_entry"] = f"{state['pcr_at_entry']:.3f}"
        if isinstance(state.get("last_pcr"), (int, float)):
            position["total_oi_pcr_last_close"] = f"{state['last_pcr']:.3f}" + (f" @ {state['last_pcr_at']}" if state.get("last_pcr_at") else "")
    if not isinstance(position, dict):
        return None

    if isinstance(position.get("legs"), dict):
        raw_legs: list[tuple[str, dict]] = [(leg["side"], leg) for leg in position["legs"].values()]
    elif "short" in position and "long" in position:
        raw_legs = [("sell", position["short"]), ("buy", position["long"])]
    else:
        return None

    legs_out: list[NativeLegOut] = []
    prices: list[float | None] = []
    leg_instruments: list[Instrument] = []
    for side, leg in raw_legs:
        instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        if instrument is None:
            return None
        leg_instruments.append(instrument)
        current_price = tick_engine.get_current_price(instrument.id)
        prices.append(current_price)
        legs_out.append(
            NativeLegOut(
                instrument_symbol=instrument.symbol, strike=instrument.strike, option_type=instrument.option_type,
                side="short" if side == "sell" else "long", quantity=leg["quantity"], entry_price=leg["entry_price"],
                current_price=current_price, instrument_type=instrument.instrument_type,
            )
        )

    # Net credit received at entry (a "sell" leg pays you, a "buy" leg
    # costs you) -- matches each strategy's own close_position() P&L sign
    # convention exactly, verified against nifty_pcr_multi_regime's
    # per-leg (entry - exit) for sell / (exit - entry) for buy formula.
    # A future isn't a credit or a debit -- it books only its profit or loss
    # (native_runner.open_leg) -- so it counts in Unrealized P&L only, not in
    # the values: its ~Rs 1.5 crore contract value would bury the premium.
    is_future = [instrument.instrument_type == "future" for instrument in leg_instruments]
    trade_value = sum(
        (leg["entry_price"] if side == "sell" else -leg["entry_price"]) * leg["quantity"]
        for (side, leg), future in zip(raw_legs, is_future) if not future
    )
    live_value = None
    unrealized_pnl = None
    if all(p is not None for p in prices):
        live_value = sum(
            (price if side == "sell" else -price) * leg["quantity"]
            for (side, leg), price, future in zip(raw_legs, prices, is_future) if not future
        )
        futures_pnl = sum(
            ((leg["entry_price"] - price) if side == "sell" else (price - leg["entry_price"])) * leg["quantity"]
            for (side, leg), price, future in zip(raw_legs, prices, is_future) if future
        )
        unrealized_pnl = trade_value - live_value + futures_pnl

    underlying_id = leg_instruments[0].underlying_instrument_id if leg_instruments else None
    underlying = await db.get(Instrument, underlying_id) if underlying_id else None
    return NativePositionOut(
        bias=position.get("bias") or position.get("regime"), opened_at=datetime.fromisoformat(position["opened_at"]),
        legs=legs_out, trade_value=trade_value, live_value=live_value, unrealized_pnl=unrealized_pnl,
        metrics=_scalar_metrics(position, _POSITION_CORE_KEYS),
        underlying_symbol=underlying.symbol if underlying else None,
        underlying_price=tick_engine.get_current_price(underlying.id) if underlying else None,
    )


# Keys every holding carries (see nifty_rs_rotation.py's holdings shape) --
# anything else a strategy stores on a holding is its own per-stock metric.
_HOLDING_CORE_KEYS = {"instrument_id", "quantity", "entry_price", "opened_at"}
# Same for a position: its legs and the fields NativePositionOut already
# shows (bias/regime, opened_at) -- anything else is the strategy's own.
_POSITION_CORE_KEYS = {"legs", "short", "long", "opened_at", "bias", "regime"}


def _scalar_metrics(entry: dict, core_keys: set[str]) -> dict:
    return {
        key: value for key, value in entry.items()
        if key not in core_keys and (value is None or isinstance(value, (bool, int, float, str)))
    }


def _parse_opened_at(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


async def _build_holdings_out(db: AsyncSession, state: dict | None) -> list[NativeHoldingOut] | None:
    """Display-only reconstruction of deployment.state["holdings"] -- a
    dict of independently-opened long equity positions (see
    NativeDeploymentOut.holdings' docstring for why this is separate from
    _build_position_out's single-spread shape). None when the state
    doesn't have this shape at all (flat, or a single-position strategy)
    so the API can tell "definitely nothing held" from "holds something,
    just not this shape" -- an empty list means genuinely flat under this
    convention specifically."""
    holdings = (state or {}).get("holdings") if state else None
    if not isinstance(holdings, dict):
        return None

    legs_out: list[NativeHoldingOut] = []
    for leg in holdings.values():
        instrument = await db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        if instrument is None:
            continue
        current_price = tick_engine.get_current_price(instrument.id)
        legs_out.append(
            NativeHoldingOut(
                instrument_symbol=instrument.symbol, strike=instrument.strike, option_type=instrument.option_type,
                side="long", quantity=leg["quantity"], entry_price=leg["entry_price"], current_price=current_price,
                opened_at=_parse_opened_at(leg.get("opened_at")),
                metrics=_scalar_metrics(leg, _HOLDING_CORE_KEYS),
            )
        )
    return legs_out


async def _get_owned_portfolio(db: AsyncSession, user: User, portfolio_id: str) -> PaperPortfolio:
    portfolio = await db.get(PaperPortfolio, uuid.UUID(portfolio_id))
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capital pool not found")
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your capital pool")
    return portfolio


async def _get_owned_native_deployment(db: AsyncSession, user: User, deployment_id: str) -> tuple[PaperNativeDeployment, PaperPortfolio]:
    deployment = await db.get(PaperNativeDeployment, uuid.UUID(deployment_id))
    if deployment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    if portfolio.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your deployment")
    return deployment, portfolio


def _public_state(state: dict | None) -> dict | None:
    """A strategy's state without its private keys (a leading "_", e.g. a
    rotation's rolling prices for 500 stocks) -- the card never shows them."""
    if state is None:
        return None
    return {k: v for k, v in state.items() if not k.startswith("_")}


async def _deployment_out(db: AsyncSession, deployment: PaperNativeDeployment) -> NativeDeploymentOut:
    return (await _deployment_outs_batch(db, [deployment]))[0]


async def _deployment_outs_batch(db: AsyncSession, deployments: list[PaperNativeDeployment]) -> list[NativeDeploymentOut]:
    if not deployments:
        return []
    strategy_ids = {d.strategy_id for d in deployments}
    portfolio_ids = {d.portfolio_id for d in deployments}
    strategies = {s.id: s for s in (await db.execute(select(Strategy).where(Strategy.id.in_(strategy_ids)))).scalars()}
    portfolios = {p.id: p for p in (await db.execute(select(PaperPortfolio).where(PaperPortfolio.id.in_(portfolio_ids)))).scalars()}
    running = (
        await db.execute(
            select(StrategyVersion.id, StrategyVersion.version_number, StrategyVersion.python_code.like("%force_exit%"))
            .where(StrategyVersion.id.in_({d.strategy_version_id for d in deployments}))
        )
    ).all()
    running_versions = {version_id: number for version_id, number, _ in running}
    can_exit = {version_id: bool(exits) for version_id, _, exits in running}
    latest_versions = dict(
        (await db.execute(
            select(StrategyVersion.strategy_id, func.max(StrategyVersion.version_number))
            .where(StrategyVersion.strategy_id.in_(strategy_ids))
            .group_by(StrategyVersion.strategy_id)
        )).all()
    )
    out: list[NativeDeploymentOut] = []
    for d in deployments:
        position = await _build_position_out(db, d.state)
        holdings = await _build_holdings_out(db, d.state)
        out.append(
            NativeDeploymentOut(
                id=str(d.id), strategy_id=str(d.strategy_id), strategy_name=strategies[d.strategy_id].name,
                portfolio_id=str(d.portfolio_id), portfolio_name=portfolios[d.portfolio_id].name,
                currency=portfolios[d.portfolio_id].currency, status=d.status, last_evaluated_at=d.last_evaluated_at,
                last_signal=d.last_signal, last_signal_reason=d.last_signal_reason, state=_public_state(d.state),
                position=position, holdings=holdings, version_number=running_versions.get(d.strategy_version_id),
                latest_version_number=latest_versions.get(d.strategy_id), can_exit=can_exit.get(d.strategy_version_id, False),
                next_check_at=paper_trading_scheduler.next_wakeup(d.id) if d.status == DeploymentStatus.ACTIVE.value else None,
                created_at=d.created_at, stopped_at=d.stopped_at,
            )
        )
    return out


@router.post("/native-deployments", response_model=NativeDeploymentOut, status_code=status.HTTP_201_CREATED)
async def start_native_deployment(
    payload: NativeDeploymentCreate, db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("administrator", "trader", "analyst")),
) -> NativeDeploymentOut:
    strategy = await db.get(Strategy, uuid.UUID(payload.strategy_id))
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    if strategy.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the owner of this strategy")
    if strategy.code_type != "native":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only Advanced Python (native) strategies deploy this way")

    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number.desc()).limit(1)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy has no versions")

    portfolio = await _get_owned_portfolio(db, user, payload.portfolio_id)

    # Native strategies have no backtest step to earn -- they can't run
    # through the single-instrument backtest engine at all (see
    # native_runner.py's docstring: no fixed instrument, real DB access,
    # multi-leg positions, none of which generate_signal(candles, params)
    # can express). Starting paper trading is the first real, earned
    # milestone available for this strategy type, so DRAFT -> PAPER_TRADING
    # is allowed directly here rather than via the general state machine,
    # which stays strict -- "nothing fabricates a backtested strategy" --
    # for every other strategy type.
    current_status = StrategyStatus(strategy.status)
    if current_status == StrategyStatus.DRAFT:
        strategy.status = StrategyStatus.PAPER_TRADING.value
    elif can_transition(current_status, StrategyStatus.PAPER_TRADING):
        strategy.status = StrategyStatus.PAPER_TRADING.value

    deployment = PaperNativeDeployment(
        portfolio_id=portfolio.id, strategy_id=strategy.id, strategy_version_id=version.id,
        status=DeploymentStatus.ACTIVE.value, state=None,
    )
    db.add(deployment)
    await db.flush()

    await write_audit_log(
        db, user_id=user.id, action="PAPER_NATIVE_TRADING_STARTED", object_type="strategy", object_id=str(strategy.id),
        new_value={"portfolio_id": str(portfolio.id)},
    )
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.get("/native-deployments", response_model=list[NativeDeploymentOut])
async def list_native_deployments(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> list[NativeDeploymentOut]:
    result = await db.execute(
        select(PaperNativeDeployment)
        .join(PaperPortfolio, PaperNativeDeployment.portfolio_id == PaperPortfolio.id)
        .where(PaperPortfolio.user_id == user.id)
        .order_by(
            PaperNativeDeployment.display_order.is_not(None), PaperNativeDeployment.display_order,
            PaperNativeDeployment.created_at.desc(),
        )
    )
    return await _deployment_outs_batch(db, list(result.scalars().all()))


@router.put("/native-deployments/order", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_native_deployments(
    payload: NativeDeploymentOrderIn, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> None:
    """Saves the order the owner arranged their deployment cards in (the
    up/down arrows on each card). Ids that aren't the caller's own are
    ignored rather than rejected, so a list that raced a delete still saves."""
    ids = []
    for raw in payload.deployment_ids:
        try:
            ids.append(uuid.UUID(raw))
        except ValueError:
            continue
    owned = {
        d.id: d for d in (
            await db.execute(
                select(PaperNativeDeployment)
                .join(PaperPortfolio, PaperNativeDeployment.portfolio_id == PaperPortfolio.id)
                .where(PaperPortfolio.user_id == user.id, PaperNativeDeployment.id.in_(ids))
            )
        ).scalars()
    }
    for position, deployment_id in enumerate(i for i in ids if i in owned):
        owned[deployment_id].display_order = position
    await db.commit()


def _has_open_positions(state: dict | None) -> bool:
    return bool((state or {}).get("position")) or bool((state or {}).get("holdings"))


@router.post("/native-deployments/{deployment_id}/stop", response_model=NativeDeploymentOut)
async def stop_native_deployment(
    deployment_id: str, exit_positions: bool = False, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> NativeDeploymentOut:
    """A stopped deployment isn't run at all, so whatever it holds is left as
    it is -- its exit rules, even a 3:00 PM close, don't run until it's
    restarted (30 Sep: AM OP TRD 15 MIN stopped at 11:14 kept its straddle
    open past 15:00). With exit_positions, the strategy's own exit closes
    everything first (at live prices, as the Exit button does); if it can't
    -- its code has no exit, or a price isn't available -- nothing is
    stopped and the reason comes back."""
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    exited = False
    if exit_positions and _has_open_positions(deployment.state):
        version = await db.get(StrategyVersion, deployment.strategy_version_id)
        if version is None or "force_exit" not in (version.python_code or ""):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This strategy can't close its own positions -- stop it and keep them, or let its exit rules close them first.",
            )
        outcome = await exit_native_deployment_now(db, deployment)
        await db.refresh(deployment)
        if _has_open_positions(deployment.state):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Couldn't close the open positions ({outcome.reason or outcome.action}) -- the strategy is still running.",
            )
        exited = True
    deployment.status = DeploymentStatus.STOPPED.value
    deployment.stopped_at = datetime.now(timezone.utc)
    await write_audit_log(
        db, user_id=user.id, action="PAPER_NATIVE_TRADING_STOPPED", object_type="paper_native_deployment", object_id=str(deployment.id),
        new_value={"exited_positions": exited, "left_open": _has_open_positions(deployment.state)},
    )
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.post("/native-deployments/{deployment_id}/restart", response_model=NativeDeploymentOut)
async def restart_native_deployment(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> NativeDeploymentOut:
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    if deployment.status == DeploymentStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Deployment is already active.")
    deployment.status = DeploymentStatus.ACTIVE.value
    deployment.stopped_at = None
    await write_audit_log(db, user_id=user.id, action="PAPER_NATIVE_TRADING_RESTARTED", object_type="paper_native_deployment", object_id=str(deployment.id))
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.post("/native-deployments/{deployment_id}/use-latest-version", response_model=NativeDeploymentOut)
async def use_latest_strategy_version(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> NativeDeploymentOut:
    """Moves a deployment onto its strategy's newest saved version, keeping
    its state -- holdings, open position, "seeded" and so on -- so the new
    code carries on from where the old code left off at its next tick.
    Otherwise a deployment runs the version it was started with for good:
    saving in the Strategy Builder only adds a version, and Restart keeps
    the old one. The new code has to understand the state the old code
    left behind (same keys, same shapes)."""
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    latest = (
        await db.execute(
            select(StrategyVersion).where(StrategyVersion.strategy_id == deployment.strategy_id)
            .order_by(StrategyVersion.version_number.desc()).limit(1)
        )
    ).scalar_one()
    if latest.id == deployment.strategy_version_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already running the latest version.")
    previous = await db.get(StrategyVersion, deployment.strategy_version_id)
    deployment.strategy_version_id = latest.id
    # The card has one version: its live run, if on, moves with it (same
    # state-compatibility rule as paper -- the new code carries on from it).
    live = await linked_run(db, deployment.id)
    if live is not None:
        live.strategy_version_id = latest.id
    await write_audit_log(
        db, user_id=user.id, action="PAPER_NATIVE_VERSION_UPDATED", object_type="paper_native_deployment", object_id=str(deployment.id),
        previous_value={"version_number": previous.version_number if previous else None}, new_value={"version_number": latest.version_number},
    )
    await db.commit()
    await db.refresh(deployment)
    return await _deployment_out(db, deployment)


@router.delete("/native-deployments/{deployment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_native_deployment(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    if deployment.status == DeploymentStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stop this deployment before deleting it.")
    if await linked_run(db, deployment.id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="It's live -- turn Live off before deleting it.")

    await db.execute(delete(PaperNativeTrade).where(PaperNativeTrade.deployment_id == deployment.id))
    await write_audit_log(
        db, user_id=user.id, action="PAPER_NATIVE_DEPLOYMENT_DELETED", object_type="paper_native_deployment", object_id=str(deployment.id),
    )
    await db.delete(deployment)
    await db.commit()


@router.post("/native-deployments/{deployment_id}/evaluate", response_model=NativeEvaluationOut)
async def evaluate_native_deployment_now(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> NativeEvaluationOut:
    """Manual trigger, same shape as the single-instrument engine's
    /evaluate -- the background scheduler calls the exact same
    run_native_strategy()."""
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    outcome = await run_native_strategy(db, deployment)
    return NativeEvaluationOut(action=outcome.action, signal=outcome.signal, reason=outcome.reason)


@router.post("/native-deployments/{deployment_id}/exit", response_model=NativeEvaluationOut)
async def exit_native_deployment_now_endpoint(deployment_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> NativeEvaluationOut:
    """Asks the strategy's own evaluate() to force-close whatever it
    currently holds (state["force_exit"]) rather than trying to unwind an
    arbitrary legs shape generically from outside the strategy's code."""
    deployment, _portfolio = await _get_owned_native_deployment(db, user, deployment_id)
    outcome = await exit_native_deployment_now(db, deployment)
    return NativeEvaluationOut(action=outcome.action, signal=outcome.signal, reason=outcome.reason)


async def _trade_outs(db: AsyncSession, rows: list[tuple[PaperNativeTrade, str, str]]) -> list[NativeTradeOut]:
    """(trade, strategy name, pool currency) rows -> readable records.
    Legs saved before record_trade() snapshotted contract details get them
    resolved here instead, batched across every trade, and their charges
    estimated the same way record_trade() now does at close."""
    resolved_legs = await resolve_leg_details(db, [t.legs or [] for t, _name, _currency in rows])
    out: list[NativeTradeOut] = []
    for (t, name, currency), legs in zip(rows, resolved_legs):
        charges = t.charges if t.charges is not None else estimate_charges(legs, t.opened_at, t.closed_at)
        out.append(
            NativeTradeOut(
                id=str(t.id), deployment_id=str(t.deployment_id), strategy_name=name, currency=currency,
                opened_at=as_aware_utc(t.opened_at), closed_at=as_aware_utc(t.closed_at), **summarize_trade(legs),
                pnl=t.pnl, charges=charges, net_pnl=t.pnl - (charges or 0.0), pnl_pct=t.pnl_pct, exit_reason=t.exit_reason,
            )
        )
    return out


@router.get("/native-trades", response_model=list[NativeTradeOut])
async def list_native_trades(
    deployment_id: str | None = None, limit: int = 200,
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user),
) -> list[NativeTradeOut]:
    stmt = (
        select(PaperNativeTrade, Strategy.name, PaperPortfolio.currency)
        .join(PaperNativeDeployment, PaperNativeTrade.deployment_id == PaperNativeDeployment.id)
        .join(PaperPortfolio, PaperNativeDeployment.portfolio_id == PaperPortfolio.id)
        .join(Strategy, PaperNativeDeployment.strategy_id == Strategy.id)
        .order_by(PaperNativeTrade.closed_at.desc())
    )
    if deployment_id:
        await _get_owned_native_deployment(db, user, deployment_id)
        stmt = stmt.where(PaperNativeTrade.deployment_id == uuid.UUID(deployment_id))
    else:
        stmt = stmt.where(PaperPortfolio.user_id == user.id).limit(limit)
    rows = [tuple(row) for row in (await db.execute(stmt)).all()]
    return await _trade_outs(db, rows)
