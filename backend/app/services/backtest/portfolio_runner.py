import asyncio
import uuid
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.models.backtest import (
    BacktestStatus,
    PortfolioBacktestJob,
    PortfolioBacktestResult,
    PortfolioBacktestTrade,
)
from app.models.strategy import Strategy, StrategyVersion
from app.services.backtest.candle_source import load_candles
from app.services.backtest.engine import CostConfig, RiskRules
from app.services.backtest.metrics import compute_metrics
from app.services.backtest.portfolio_engine import PortfolioSizing, as_metrics_input, simulate_portfolio
from app.services.backtest.signals import (
    SignalComputationError,
    compute_python_signals_batch,
    compute_visual_signals,
)
from app.services.strategy.state_machine import StrategyStatus, can_transition

MAX_CANDLES = 3000  # per instrument -- bounds worst-case runtime of the O(n^2) visual-mode signal computation
MIN_CANDLES = 30

# A Python-strategy portfolio backtest used to spawn one subprocess (and
# redundantly recompile identical code) per instrument, fully sequential --
# 500 instruments meant 500 process spawns, confirmed live to take 24+
# hours instead of the ~2 minutes the same workload took at a smaller
# instrument count. Batching + bounded concurrency is the actual fix;
# picked conservatively for the real (modest, shared) production VPS, not
# tuned for a beefier box that isn't this one.
PYTHON_BATCH_SIZE = 25
MAX_CONCURRENT_BATCHES = 4
# Outer safety net: whatever the cause, a job must never sit at "running"
# indefinitely again the way today's two did -- this wraps the whole
# signal-computation phase, on top of (not instead of) each batch's own
# scaled per-call timeout (sandbox.py's run_python_portfolio_backtest_signals).
JOB_MAX_SECONDS = 45 * 60


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def run_portfolio_backtest_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(PortfolioBacktestJob, job_id)
        if job is None:
            return

        job.status = BacktestStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            version = await db.get(StrategyVersion, job.strategy_version_id)
            if version is None:
                raise SignalComputationError("Strategy version not found")

            signals_by_instrument: dict[str, object] = {}
            skipped_symbols: list[str] = []
            # Kept separate from the generic "not enough candles" skip reason
            # so a strategy-code bug (e.g. a Python strategy that fails the
            # sandbox for every instrument alike) surfaces its real cause
            # instead of the misleading "insufficient data" message -- both
            # reasons land the same instrument in skipped_symbols, but only
            # one of them means the data itself was the problem.
            last_signal_error: str | None = None

            from app.models.instrument import Instrument  # local import: avoid a module-level cycle with strategy models

            # Phase 1: load every instrument's candles -- DB-bound, stays
            # sequential (AsyncSession isn't safe for concurrent use across
            # tasks, and this was never the bottleneck; confirmed live that
            # the per-instrument SANDBOX call was).
            inst_by_id: dict[str, Instrument] = {}
            candles_needing_signals: dict[str, list] = {}
            for raw_id in job.instrument_ids:
                instrument = await db.get(Instrument, uuid.UUID(raw_id))
                if instrument is None:
                    continue
                candles = (await load_candles(db, instrument.id, job.timeframe, job.start_date, job.end_date))[-MAX_CANDLES:]
                if len(candles) < MIN_CANDLES:
                    skipped_symbols.append(instrument.symbol)
                    continue
                inst_id = str(instrument.id)
                inst_by_id[inst_id] = instrument
                candles_needing_signals[inst_id] = candles

            # Phase 2: compute signals. A strategy is either python or visual
            # for its whole job (never mixed per-instrument), so this
            # branches once, not per instrument.
            if version.python_code and candles_needing_signals:
                batches = _chunk(list(candles_needing_signals.items()), PYTHON_BATCH_SIZE)
                semaphore = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)

                async def _run_batch(batch: list[tuple[str, list]]) -> tuple[dict, dict]:
                    async with semaphore:
                        return await compute_python_signals_batch(dict(batch), version.python_code, version.parameters)

                try:
                    batch_results = await asyncio.wait_for(
                        asyncio.gather(*(_run_batch(b) for b in batches)), timeout=JOB_MAX_SECONDS,
                    )
                except asyncio.TimeoutError:
                    raise SignalComputationError(
                        f"Signal computation exceeded the maximum job runtime ({JOB_MAX_SECONDS // 60} minutes) -- "
                        "try a smaller instrument selection."
                    )
                for batch_signals, batch_errors in batch_results:
                    signals_by_instrument.update(batch_signals)
                    for inst_id, err in batch_errors.items():
                        last_signal_error = err
                        skipped_symbols.append(inst_by_id[inst_id].symbol)
            elif candles_needing_signals:
                for inst_id, candles in candles_needing_signals.items():
                    try:
                        signals_by_instrument[inst_id] = compute_visual_signals(candles, version.entry_rules, version.exit_rules)
                    except SignalComputationError as exc:
                        last_signal_error = str(exc)
                        skipped_symbols.append(inst_by_id[inst_id].symbol)

            instruments = [inst_by_id[inst_id] for inst_id in signals_by_instrument]
            candles_by_instrument = {inst_id: candles_needing_signals[inst_id] for inst_id in signals_by_instrument}

            if not instruments:
                if last_signal_error:
                    raise SignalComputationError(
                        f"The strategy failed to compute a signal for every selected instrument: {last_signal_error}"
                    )
                raise SignalComputationError(
                    "None of the selected instruments have enough backfilled candles "
                    f"(need at least {MIN_CANDLES}) at timeframe '{job.timeframe}' to run a portfolio backtest"
                )

            sizing = PortfolioSizing(position_size_pct=job.position_size_pct, max_open_positions=job.max_open_positions)
            risk = RiskRules(
                stop_loss_pct=version.risk_rules.get("stop_loss_pct"),
                take_profit_pct=version.risk_rules.get("take_profit_pct"),
            )
            costs = CostConfig(brokerage_pct=job.brokerage_pct, slippage_pct=job.slippage_pct, tax_pct=job.tax_pct)

            output = simulate_portfolio(
                instruments, candles_by_instrument, signals_by_instrument, job.initial_capital, sizing, risk, costs,
                breadth_exit_threshold=job.breadth_exit_threshold,
            )
            metrics = compute_metrics(as_metrics_input(output, job.initial_capital), job.initial_capital, job.timeframe)

            db.add(
                PortfolioBacktestResult(
                    job_id=job.id, metrics=metrics,
                    equity_curve=[[ts.isoformat(), equity] for ts, equity in output.equity_curve],
                    instrument_count=len(instruments), skipped_symbols=skipped_symbols,
                )
            )
            for trade in output.trades:
                db.add(
                    PortfolioBacktestTrade(
                        job_id=job.id, instrument_id=uuid.UUID(trade.instrument_id), symbol=trade.symbol,
                        entry_ts=trade.entry_ts, entry_price=trade.entry_price, exit_ts=trade.exit_ts,
                        exit_price=trade.exit_price, quantity=trade.quantity, pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                        bars_held=trade.bars_held, exit_reason=trade.exit_reason, status=trade.status, side=trade.side,
                    )
                )

            job.status = BacktestStatus.COMPLETED.value
            job.completed_at = datetime.now(timezone.utc)

            strategy = await db.get(Strategy, job.strategy_id)
            if strategy is not None and can_transition(StrategyStatus(strategy.status), StrategyStatus.BACKTESTED):
                strategy.status = StrategyStatus.BACKTESTED.value

            await db.commit()

        except SignalComputationError as exc:
            job.status = BacktestStatus.FAILED.value
            job.error_message = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
        except Exception as exc:  # a backtest job must never leave "running" stuck on an unexpected bug
            job.status = BacktestStatus.FAILED.value
            job.error_message = f"{type(exc).__name__}: {exc}"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
