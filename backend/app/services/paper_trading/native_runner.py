"""Runs "Advanced Python" strategies (`Strategy.code_type == "native"`) --
trusted, unsandboxed Python for strategies the sandbox's
`generate_signal(candles, params) -> one signal` contract can't express:
no fixed instrument (the strategy picks its own each tick), real DB
access (PCR/OI/option-chain lookups), multi-leg positions.

Deliberately NOT run through services/strategy/sandbox.py -- no
RestrictedPython, no subprocess. This is appropriate specifically
because these strategies aren't arbitrary other-user-submitted code that
needs isolating; they're vetted, predefined strategies dropped in here
on purpose, the same trust level as any other backend service module
(see services/options/pcr.py, services/risk/engine.py).

A pasted module must define `async def evaluate(ctx) -> None`. `ctx`
(NativeContext below) is the entire surface such a strategy gets: no
direct AsyncSession leakage into "just do whatever" territory beyond
what these helpers cover, so the same auditability/alerting/cash
conventions the single-instrument engine (engine.py) already has stay
consistent here too.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import AlertSeverity, AlertType
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import PaperNativeDeployment, PaperNativeTrade, PaperPortfolio
from app.models.strategy import StrategyVersion
from app.services.alerts.service import create_alert
from app.services.audit import write_audit_log
from app.services.market_data.tick_engine import tick_engine
from app.services.options.pcr import compute_effective_pcr

logger = logging.getLogger(__name__)


@dataclass
class EvaluationOutcome:
    action: str  # "entered" | "exited" | "rolled" | "hold" | "error" | "skipped"
    signal: str | None = None
    reason: str | None = None


class NativeContext:
    """Everything a pasted `evaluate(ctx)` strategy can do. `state` is a
    plain dict, fully owned by the strategy's own code -- the runner only
    persists whatever is in it back onto the deployment row after each
    tick, never interprets its shape."""

    # True only on backtest/native_runner.py's BacktestNativeContext -- lets
    # a strategy skip live-only side effects (a live Kite fetch, a Telegram
    # push) during a historical replay.
    is_backtest = False

    def __init__(
        self, db: AsyncSession, portfolio: PaperPortfolio, deployment: PaperNativeDeployment, state: dict,
        now: datetime | None = None,
    ):
        self.db = db
        self.portfolio = portfolio
        self.deployment = deployment
        self.state = state
        # Injectable so a strategy's wall-clock-gated logic (entry/exit
        # windows) is deterministically testable -- defaults to the real
        # current time in production (see run_native_strategy below),
        # never faked outside tests.
        self.now: datetime = now or datetime.now(timezone.utc)
        self._last_signal: str | None = None
        self._last_reason: str | None = None
        self._last_action: str = "hold"

    def note(self, action: str, signal: str | None = None, reason: str | None = None) -> None:
        """The strategy calls this once per tick to report what it did --
        persisted onto the deployment's last_signal/last_signal_reason by
        the runner, same visibility the single-instrument engine's
        last_signal column already gives every other deployment."""
        self._last_action = action
        self._last_signal = signal
        self._last_reason = reason

    async def get_price(self, instrument_id: uuid.UUID) -> float | None:
        """Real tick if TickEngine has one (Kite WS, the REST fallback, or
        Delta), else the latest stored candle close at any timeframe --
        same two-step fallback engine.py's evaluate_deployment already
        uses for its single instrument. Also (re-)subscribes the
        instrument with TickEngine so the live feeds start/keep tracking
        it -- the same unconditional per-tick `subscribe()` call
        paper_trading/scheduler.py's existing loop already makes for its
        one fixed instrument; TickEngine only ever reads this as
        count > 0, so calling it every tick (rather than tracking whether
        we already did) matches that existing convention exactly."""
        price = tick_engine.get_current_price(instrument_id)
        if price is None:
            row = (
                await self.db.execute(
                    select(OhlcvCandle.close)
                    .where(OhlcvCandle.instrument_id == instrument_id)
                    .order_by(OhlcvCandle.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            price = row
        tick_engine.subscribe(instrument_id, seed_price=price or 0.0)
        return price

    async def get_pcr(self, underlying_symbol: str = "NIFTY 50", num_expiries: int = 4, timeframe: str = "15m") -> float | None:
        return await compute_effective_pcr(self.db, underlying_symbol=underlying_symbol, num_expiries=num_expiries, timeframe=timeframe)

    async def find_option(self, underlying_instrument_id: uuid.UUID, expiry: date, strike: float, option_type: str) -> Instrument | None:
        return (
            await self.db.execute(
                select(Instrument).where(
                    Instrument.underlying_instrument_id == underlying_instrument_id,
                    Instrument.expiry == expiry,
                    Instrument.strike == strike,
                    Instrument.option_type == option_type,
                    Instrument.instrument_type == "option",
                )
            )
        ).scalar_one_or_none()

    async def list_weekly_expiries(self, underlying_instrument_id: uuid.UUID, today: date, limit: int = 4) -> list[date]:
        rows = (
            await self.db.execute(
                select(Instrument.expiry)
                .where(
                    Instrument.underlying_instrument_id == underlying_instrument_id,
                    Instrument.instrument_type == "option",
                    Instrument.expiry.is_not(None),
                    Instrument.expiry >= today,
                )
                .distinct()
                .order_by(Instrument.expiry)
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)

    async def _move_cash(self, side: str, quantity: float, price: float, action: str, instrument: Instrument) -> None:
        notional = quantity * price
        self.portfolio.cash += notional if side == "sell" else -notional
        await write_audit_log(
            self.db, user_id=self.portfolio.user_id, action=action, object_type="paper_native_deployment",
            object_id=str(self.deployment.id),
            new_value={"instrument": instrument.symbol, "side": side, "quantity": quantity, "price": price},
        )

    async def open_leg(self, instrument: Instrument, side: str, quantity: float, price: float) -> None:
        """side: "sell" (credits cash -- selling to open, e.g. a spread's
        short leg) or "buy" (debits cash -- buying to open, e.g. a
        spread's long hedge leg). Mirrors engine.py::_try_enter's
        short-entry cash convention, applied per leg."""
        await self._move_cash(side, quantity, price, "PAPER_NATIVE_LEG_OPENED", instrument)

    async def close_leg(self, instrument: Instrument, side: str, quantity: float, price: float) -> None:
        """side is the CLOSING action -- "buy" to cover a short leg,
        "sell" to close a long leg. Mirrors engine.py::_exit_position's
        cash convention, applied per leg."""
        await self._move_cash(side, quantity, price, "PAPER_NATIVE_LEG_CLOSED", instrument)

    async def record_trade(
        self, legs: list[dict], pnl: float, pnl_pct: float, exit_reason: str, opened_at: datetime, closed_at: datetime | None = None,
    ) -> None:
        """`legs`: [{"instrument_id": str, "side": "short"|"long", "quantity": float, "entry_price": float, "exit_price": float}, ...]"""
        self.db.add(
            PaperNativeTrade(
                deployment_id=self.deployment.id, opened_at=opened_at, closed_at=closed_at or datetime.now(timezone.utc),
                legs=legs, pnl=pnl, pnl_pct=pnl_pct, exit_reason=exit_reason,
            )
        )
        strategy_name = self.deployment.strategy_id  # resolved to a name by the caller if it wants a nicer alert title
        await create_alert(
            self.db, user_id=self.portfolio.user_id, alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO,
            title="Paper spread closed", message=f"{exit_reason}: P&L {pnl:+.2f}",
            object_type="paper_native_deployment", object_id=str(self.deployment.id),
        )


async def run_native_strategy(db: AsyncSession, deployment: PaperNativeDeployment) -> EvaluationOutcome:
    """Thin wrapper mirroring engine.py::evaluate_deployment: persists
    last_signal/last_signal_reason from whatever happened, regardless of
    which path produced it."""
    outcome = await _run_native_strategy(db, deployment)
    label = (outcome.signal or outcome.action.upper())[:20]
    reason = outcome.reason[:500] if outcome.reason else None
    if deployment.last_signal != label or deployment.last_signal_reason != reason:
        deployment.last_signal = label
        deployment.last_signal_reason = reason
        await db.commit()
    return outcome


async def _run_native_strategy(db: AsyncSession, deployment: PaperNativeDeployment) -> EvaluationOutcome:
    version = await db.get(StrategyVersion, deployment.strategy_version_id)
    portfolio = await db.get(PaperPortfolio, deployment.portfolio_id)
    if version is None or portfolio is None:
        return EvaluationOutcome(action="error", reason="deployment references missing data")
    if not version.python_code:
        return EvaluationOutcome(action="error", reason="native strategy has no code")

    deployment.last_evaluated_at = datetime.now(timezone.utc)

    module_ns: dict = {}
    try:
        compiled = compile(version.python_code, filename=f"<native-strategy:{deployment.strategy_id}>", mode="exec")
        exec(compiled, module_ns)
    except Exception as exc:
        await db.commit()
        return EvaluationOutcome(action="error", reason=f"{type(exc).__name__}: code failed to load: {exc}")

    evaluate_fn = module_ns.get("evaluate")
    if not callable(evaluate_fn):
        await db.commit()
        return EvaluationOutcome(action="error", reason="native strategy code must define async def evaluate(ctx)")

    ctx = NativeContext(db=db, portfolio=portfolio, deployment=deployment, state=dict(deployment.state or {}))
    try:
        await evaluate_fn(ctx)
    except Exception as exc:
        logger.exception("Native strategy evaluation failed for deployment %s", deployment.id)
        deployment.state = ctx.state
        await db.commit()
        return EvaluationOutcome(action="error", reason=f"{type(exc).__name__}: {exc}")

    deployment.state = ctx.state
    await db.commit()
    return EvaluationOutcome(action=ctx._last_action, signal=ctx._last_signal, reason=ctx._last_reason)


async def exit_native_deployment_now(db: AsyncSession, deployment: PaperNativeDeployment) -> EvaluationOutcome:
    """Manual exit hook -- calls the strategy's own evaluate() but with
    state["force_exit"] set, matching the convention a native strategy is
    expected to check at the top of its own exit logic. Simpler than
    trying to generically unwind an arbitrary legs shape from outside the
    strategy's own code, which only the strategy itself truly understands."""
    state = dict(deployment.state or {})
    state["force_exit"] = True
    deployment.state = state
    return await run_native_strategy(db, deployment)
