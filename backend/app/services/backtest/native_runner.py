"""Historical replay backtest for "native" (Advanced Python) strategies --
BacktestJob's sibling for `Strategy.code_type == "native"`, which the
regular engine (engine.py/portfolio_engine.py) can't run at all: no fixed
instrument, real DB access (PCR/OI/option-chain lookups), multi-leg
positions -- none of which fit `generate_signal(candles, params)`.

Reuses the exact same `evaluate(ctx)` code path
paper_trading/native_runner.py runs live (same compile/exec load, same
NativeContext method surface via BacktestNativeContext below) so backtest
behavior can't drift from live/paper behavior -- only the context's data
sources change (historical DB reads instead of live ticks) and its side
effects are redirected (record_trade collects into memory instead of
writing PaperNativeTrade + sending a live alert, cash bookkeeping skips
the audit log so a replay doesn't pollute the real audit trail).
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.backtest import BacktestStatus, NativeBacktestJob, NativeBacktestResult, NativeBacktestTrade
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import DeploymentStatus, PaperNativeDeployment, PaperPortfolio
from app.models.strategy import Strategy, StrategyVersion
from app.services.broker.zerodha_broker import IST
from app.services.market_data.bar_periods import load_closed_candles
from app.services.market_data.hours import nse_market_open
from app.services.options.pcr import compute_effective_pcr
from app.services.paper_trading.native_runner import NativeContext
from app.services.strategy.state_machine import StrategyStatus, can_transition

# 5m matches fo_opening_momentum.py's own CANDLE_TIMEFRAME and is finer than
# nifty_pcr_credit_spread.py's 15m PCR cadence, so one replay interval
# serves both strategies without either missing a decision window.
REPLAY_STEP = timedelta(minutes=5)
MARKET_OPEN_IST = time(9, 15)
MARKET_CLOSE_IST = time(15, 30)


class BacktestNativeContext(NativeContext):
    """Same public method surface as NativeContext (get_price, get_pcr,
    find_option, list_weekly_expiries, open_leg, close_leg, record_trade,
    note) so a native strategy's evaluate(ctx) needs zero changes to run
    here -- only the three methods below differ from live behavior."""

    is_backtest = True

    def __init__(self, db: AsyncSession, portfolio: PaperPortfolio, deployment: PaperNativeDeployment, state: dict, now: datetime):
        super().__init__(db=db, portfolio=portfolio, deployment=deployment, state=state, now=now)
        self.trades: list[dict] = []

    async def get_price(self, instrument_id: uuid.UUID) -> float | None:
        """No live tick engine in a backtest -- the latest stored candle
        close at or before the simulated instant, at any timeframe (same
        "any timeframe" convention NativeContext.get_price's live fallback
        already uses, just bounded to the past relative to `now` instead
        of unbounded)."""
        return (
            await self.db.execute(
                select(OhlcvCandle.close)
                .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.ts <= self.now)
                .order_by(OhlcvCandle.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def get_candles(self, instrument_id: uuid.UUID | str, timeframe: str, limit: int = 300) -> list[dict]:
        """Candles finished as of the replayed `now` -- nothing to keep
        fresh during a replay, so unlike the live context this doesn't
        touch the background candle sync."""
        return await load_closed_candles(self.db, uuid.UUID(str(instrument_id)), timeframe, limit, self.now)

    async def get_pcr(self, underlying_symbol: str = "NIFTY 50", num_expiries: int = 4, timeframe: str = "15m") -> float | None:
        return await compute_effective_pcr(
            self.db, underlying_symbol=underlying_symbol, num_expiries=num_expiries, timeframe=timeframe, as_of=self.now,
        )

    async def _move_cash(self, side: str, quantity: float, price: float, action: str, instrument: Instrument) -> None:
        notional = quantity * price
        self.portfolio.cash += notional if side == "sell" else -notional

    async def record_trade(
        self, legs: list[dict], pnl: float, pnl_pct: float, exit_reason: str, opened_at: datetime, closed_at: datetime | None = None,
    ) -> None:
        self.trades.append({
            "opened_at": opened_at, "closed_at": closed_at or self.now,
            "legs": legs, "pnl": pnl, "pnl_pct": pnl_pct, "exit_reason": exit_reason,
        })


def _trading_instants(start_date: date, end_date: date):
    """Every REPLAY_STEP-spaced instant across [start_date, end_date],
    09:15-15:30 IST per day -- nse_market_open (already takes an explicit
    `now`, not implicit wall-clock) filters out weekends/holidays per-day
    as this generator is consumed, exactly the same gate live scheduling
    uses (market_data/hours.py)."""
    current_date = start_date
    while current_date <= end_date:
        day_start = datetime.combine(current_date, MARKET_OPEN_IST, tzinfo=IST).astimezone(timezone.utc)
        day_end = datetime.combine(current_date, MARKET_CLOSE_IST, tzinfo=IST).astimezone(timezone.utc)
        ts = day_start
        while ts <= day_end:
            if nse_market_open(ts):
                yield ts
            ts += REPLAY_STEP
        current_date += timedelta(days=1)


async def run_native_backtest_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(NativeBacktestJob, job_id)
        if job is None:
            return

        job.status = BacktestStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            version = await db.get(StrategyVersion, job.strategy_version_id)
            if version is None or not version.python_code:
                raise ValueError("Strategy version has no code to replay")

            module_ns: dict = {}
            compiled = compile(version.python_code, filename=f"<native-backtest:{job.strategy_id}>", mode="exec")
            exec(compiled, module_ns)
            evaluate_fn = module_ns.get("evaluate")
            if not callable(evaluate_fn):
                raise ValueError("native strategy code must define async def evaluate(ctx)")

            # In-memory only -- never added to the session, so nothing here
            # touches the real paper_portfolios/paper_native_deployments
            # tables. Just enough of a stand-in for NativeContext's own
            # `self.portfolio`/`self.deployment` attribute access to work
            # unmodified.
            portfolio = PaperPortfolio(
                id=uuid.uuid4(), user_id=job.requested_by, name="Backtest", currency="INR",
                cash=job.initial_capital, initial_capital=job.initial_capital,
            )
            deployment = PaperNativeDeployment(
                id=uuid.uuid4(), portfolio_id=portfolio.id, strategy_id=job.strategy_id,
                strategy_version_id=job.strategy_version_id, status=DeploymentStatus.ACTIVE.value,
            )

            state: dict = {}
            trades: list[dict] = []
            last_ts: datetime | None = None
            instants = _trading_instants(job.start_date, job.end_date)
            for ts in instants:
                ctx = BacktestNativeContext(db=db, portfolio=portfolio, deployment=deployment, state=dict(state), now=ts)
                try:
                    await evaluate_fn(ctx)
                except Exception as exc:
                    raise RuntimeError(f"strategy evaluation failed at {ts.isoformat()}: {type(exc).__name__}: {exc}") from exc
                state = ctx.state
                trades.extend(ctx.trades)
                last_ts = ts

            # Force-close anything still open at the end of the range, the
            # same "force_exit" convention
            # paper_trading/native_runner.py::exit_native_deployment_now
            # already uses live, so the report reflects a realistic close
            # instead of an abandoned position (state shapes per
            # paper_native_trading.py's _build_position_out/_build_holdings_out).
            if last_ts is not None and (state.get("position") or state.get("holdings")):
                state["force_exit"] = True
                ctx = BacktestNativeContext(db=db, portfolio=portfolio, deployment=deployment, state=dict(state), now=last_ts)
                await evaluate_fn(ctx)
                state = ctx.state
                trades.extend(ctx.trades)

            trades.sort(key=lambda t: t["closed_at"])
            equity = job.initial_capital
            equity_curve: list[list] = [[
                datetime.combine(job.start_date, MARKET_OPEN_IST, tzinfo=IST).astimezone(timezone.utc).isoformat(),
                equity,
            ]]
            for t in trades:
                equity += t["pnl"]
                equity_curve.append([t["closed_at"].isoformat(), equity])

            wins = [t for t in trades if t["pnl"] > 0]
            metrics = {
                "trade_count": len(trades),
                "net_pnl": round(sum(t["pnl"] for t in trades), 2),
                "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
                "best_trade": round(max((t["pnl"] for t in trades), default=0.0), 2),
                "worst_trade": round(min((t["pnl"] for t in trades), default=0.0), 2),
                "final_capital": round(equity, 2),
            }

            db.add(NativeBacktestResult(job_id=job.id, metrics=metrics, equity_curve=equity_curve))
            for t in trades:
                db.add(
                    NativeBacktestTrade(
                        job_id=job.id, opened_at=t["opened_at"], closed_at=t["closed_at"], legs=t["legs"],
                        pnl=t["pnl"], pnl_pct=t["pnl_pct"], exit_reason=t["exit_reason"],
                    )
                )

            job.status = BacktestStatus.COMPLETED.value
            job.completed_at = datetime.now(timezone.utc)

            # Same unconditional-on-completion rule services/backtest/runner.py
            # already uses for regular strategies -- completion is the bar,
            # not profitability.
            strategy = await db.get(Strategy, job.strategy_id)
            if strategy is not None and can_transition(StrategyStatus(strategy.status), StrategyStatus.BACKTESTED):
                strategy.status = StrategyStatus.BACKTESTED.value

            await db.commit()

        except Exception as exc:  # a backtest job must never leave "running" stuck on an unexpected bug
            job.status = BacktestStatus.FAILED.value
            job.error_message = f"{type(exc).__name__}: {exc}"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
