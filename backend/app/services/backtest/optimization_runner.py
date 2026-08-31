import uuid
from datetime import datetime, timezone

from app.db.session import AsyncSessionLocal
from app.models.backtest import BacktestStatus, OptimizationJob, OptimizationResult
from app.models.strategy import StrategyVersion
from app.services.backtest.candle_source import load_candles
from app.services.backtest.engine import CostConfig, PositionSizing, RiskRules, simulate_trades
from app.services.backtest.metrics import compute_metrics
from app.services.backtest.optimization import GridTooLargeError, ParamRange, build_param_grid
from app.services.backtest.signals import SignalComputationError, compute_python_signals

DEFAULT_COSTS = CostConfig()


async def run_optimization_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(OptimizationJob, job_id)
        if job is None:
            return

        job.status = BacktestStatus.RUNNING.value
        await db.commit()

        try:
            version = await db.get(StrategyVersion, job.strategy_version_id)
            if not version.python_code:
                raise SignalComputationError("Optimization is only supported for Python strategies")

            candles = await load_candles(db, job.instrument_id, job.timeframe)
            if len(candles) < 30:
                raise SignalComputationError(
                    f"Not enough backfilled candles to run an optimization (need at least 30) at timeframe '{job.timeframe}'"
                    " -- try a different timeframe or backfill this one first"
                )

            try:
                grid = build_param_grid([ParamRange(**r) for r in job.param_ranges])
            except GridTooLargeError as exc:
                raise SignalComputationError(str(exc)) from exc

            sizing = PositionSizing(**version.position_sizing)
            risk = RiskRules(
                stop_loss_pct=version.risk_rules.get("stop_loss_pct"),
                take_profit_pct=version.risk_rules.get("take_profit_pct"),
            )

            runs = []
            for params in grid:
                signals = await compute_python_signals(candles, version.python_code, params)
                output = simulate_trades(candles, signals, job.initial_capital, sizing, risk, DEFAULT_COSTS)
                metrics = compute_metrics(output, job.initial_capital, job.timeframe)
                runs.append({"params": params, "metrics": metrics})

            runs.sort(key=lambda r: r["metrics"].get(job.rank_metric, 0), reverse=True)

            db.add(OptimizationResult(job_id=job.id, runs=runs))
            job.status = BacktestStatus.COMPLETED.value
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()

        except SignalComputationError as exc:
            job.status = BacktestStatus.FAILED.value
            job.error_message = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
        except Exception as exc:
            job.status = BacktestStatus.FAILED.value
            job.error_message = f"{type(exc).__name__}: {exc}"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
