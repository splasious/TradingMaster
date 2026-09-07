import uuid
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.models.backtest import (
    BacktestStatus,
    PortfolioOptimizationJob,
    PortfolioOptimizationResult,
)
from app.models.strategy import Strategy, StrategyVersion
from app.services.backtest.candle_source import load_candles
from app.services.backtest.engine import CostConfig, RiskRules
from app.services.backtest.metrics import compute_metrics
from app.services.backtest.optimization import GridTooLargeError, ParamRange, build_param_grid
from app.services.backtest.portfolio_engine import PortfolioSizing, as_metrics_input, simulate_portfolio
from app.services.backtest.signals import SignalComputationError, compute_python_signals
from app.services.strategy.state_machine import StrategyStatus, can_transition

MAX_CANDLES = 3000  # per instrument -- same cap as portfolio_runner.py, same reason
MIN_CANDLES = 30


async def _run_one_combo(instruments, candles_by_instrument, python_code, params, sizing, risk, costs, initial_capital, timeframe):
    """One parameter combination, evaluated as a real portfolio backtest
    across every instrument that has enough data -- not one instrument in
    isolation. Returns None if every instrument's signal computation
    failed for this particular combination (e.g. a parameter value the
    strategy code can't handle), so the caller can drop just this combo
    rather than fail the whole grid search."""
    signals_by_instrument: dict[str, object] = {}
    combo_instruments = []
    skipped_symbols: list[str] = []

    for instrument in instruments:
        inst_id = str(instrument.id)
        candles = candles_by_instrument[inst_id]
        try:
            signals = await compute_python_signals(candles, python_code, params)
        except SignalComputationError:
            skipped_symbols.append(instrument.symbol)
            continue
        combo_instruments.append(instrument)
        signals_by_instrument[inst_id] = signals

    if not combo_instruments:
        return None

    combo_candles = {str(i.id): candles_by_instrument[str(i.id)] for i in combo_instruments}
    output = simulate_portfolio(combo_instruments, combo_candles, signals_by_instrument, initial_capital, sizing, risk, costs)
    metrics = compute_metrics(as_metrics_input(output, initial_capital), initial_capital, timeframe)
    return {"metrics": metrics, "instrument_count": len(combo_instruments), "skipped_symbols": skipped_symbols}


async def run_portfolio_optimization_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(PortfolioOptimizationJob, job_id)
        if job is None:
            return

        job.status = BacktestStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            version = await db.get(StrategyVersion, job.strategy_version_id)
            if version is None:
                raise SignalComputationError("Strategy version not found")
            if not version.python_code:
                raise SignalComputationError(
                    "Portfolio optimization is only supported for Python strategies -- visual-mode rule "
                    "conditions use literal values, not named parameters, so there's nothing to grid-search."
                )

            try:
                grid = build_param_grid([ParamRange(**r) for r in job.param_ranges])
            except GridTooLargeError as exc:
                raise SignalComputationError(str(exc)) from exc

            from app.models.instrument import Instrument  # local import: avoid a module-level cycle with strategy models

            instruments = []
            candles_by_instrument: dict[str, list] = {}
            for raw_id in job.instrument_ids:
                instrument = await db.get(Instrument, uuid.UUID(raw_id))
                if instrument is None:
                    continue
                candles = (await load_candles(db, instrument.id, job.timeframe, job.start_date, job.end_date))[-MAX_CANDLES:]
                if len(candles) < MIN_CANDLES:
                    continue
                instruments.append(instrument)
                candles_by_instrument[str(instrument.id)] = candles

            if not instruments:
                raise SignalComputationError(
                    "None of the selected instruments have enough backfilled candles "
                    f"(need at least {MIN_CANDLES}) at timeframe '{job.timeframe}' to run a portfolio optimization"
                )

            sizing = PortfolioSizing(position_size_pct=job.position_size_pct, max_open_positions=job.max_open_positions)
            risk = RiskRules(
                stop_loss_pct=version.risk_rules.get("stop_loss_pct"),
                take_profit_pct=version.risk_rules.get("take_profit_pct"),
            )
            costs = CostConfig(brokerage_pct=job.brokerage_pct, slippage_pct=job.slippage_pct, tax_pct=job.tax_pct)

            runs = []
            for params in grid:
                combo_result = await _run_one_combo(
                    instruments, candles_by_instrument, version.python_code, params,
                    sizing, risk, costs, job.initial_capital, job.timeframe,
                )
                if combo_result is None:
                    continue
                runs.append({"params": params, **combo_result})

            if not runs:
                raise SignalComputationError(
                    "Every parameter combination failed to produce a tradeable portfolio -- "
                    "the strategy code likely can't handle one of the tested parameter values."
                )

            runs.sort(key=lambda r: r["metrics"].get(job.rank_metric, 0), reverse=True)

            db.add(PortfolioOptimizationResult(job_id=job.id, runs=runs))
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
        except Exception as exc:  # an optimization job must never leave "running" stuck on an unexpected bug
            job.status = BacktestStatus.FAILED.value
            job.error_message = f"{type(exc).__name__}: {exc}"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
