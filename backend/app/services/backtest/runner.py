import uuid
from datetime import datetime, time, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.backtest import BacktestJob, BacktestResult, BacktestStatus, BacktestTrade
from app.models.market_data import OhlcvCandle
from app.models.strategy import Strategy, StrategyVersion
from app.services.backtest.engine import CostConfig, PositionSizing, RiskRules, simulate_trades
from app.services.backtest.metrics import compute_metrics
from app.services.backtest.monte_carlo import run_monte_carlo
from app.services.backtest.signals import SignalComputationError, compute_python_signals, compute_visual_signals
from app.services.strategy.state_machine import StrategyStatus, can_transition

MAX_CANDLES = 3000  # bounds worst-case runtime of the O(n^2) visual-mode signal computation


async def run_backtest_job(job_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(BacktestJob, job_id)
        if job is None:
            return

        job.status = BacktestStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await db.commit()

        try:
            version = await db.get(StrategyVersion, job.strategy_version_id)
            candle_stmt = (
                select(OhlcvCandle)
                .where(OhlcvCandle.instrument_id == job.instrument_id, OhlcvCandle.timeframe == job.timeframe)
                .order_by(OhlcvCandle.ts)
            )
            if job.start_date:
                candle_stmt = candle_stmt.where(OhlcvCandle.ts >= datetime.combine(job.start_date, time.min, tzinfo=timezone.utc))
            if job.end_date:
                candle_stmt = candle_stmt.where(OhlcvCandle.ts <= datetime.combine(job.end_date, time.max, tzinfo=timezone.utc))
            candles_result = await db.execute(candle_stmt)
            candles = list(candles_result.scalars().all())[-MAX_CANDLES:]

            if len(candles) < 30:
                raise SignalComputationError(
                    "Not enough backfilled candles to run a backtest (need at least 30)"
                    + (" in the selected date range" if job.start_date or job.end_date else "")
                )

            if version.python_code:
                signals = await compute_python_signals(candles, version.python_code, version.parameters)
            else:
                signals = compute_visual_signals(candles, version.entry_rules, version.exit_rules)

            # A per-run sizing override (set from the Backtesting page) wins
            # over the strategy version's own baked-in sizing; otherwise
            # behavior is unchanged from before this override existed.
            if job.position_sizing_type is not None:
                sizing = PositionSizing(type=job.position_sizing_type, value=job.position_sizing_value)
            else:
                sizing = PositionSizing(**version.position_sizing)
            risk = RiskRules(
                stop_loss_pct=version.risk_rules.get("stop_loss_pct"),
                take_profit_pct=version.risk_rules.get("take_profit_pct"),
            )
            costs = CostConfig(brokerage_pct=job.brokerage_pct, slippage_pct=job.slippage_pct, tax_pct=job.tax_pct)

            output = simulate_trades(candles, signals, job.initial_capital, sizing, risk, costs)
            metrics = compute_metrics(output, job.initial_capital, job.timeframe)

            out_of_sample_metrics = None
            if job.out_of_sample_split_pct:
                split_idx = int(len(candles) * job.out_of_sample_split_pct / 100)
                if 10 < split_idx < len(candles) - 10:
                    in_sample_signals = type(signals)(entry=signals.entry[:split_idx], exit=signals.exit[:split_idx])
                    oos_signals = type(signals)(entry=signals.entry[split_idx:], exit=signals.exit[split_idx:])
                    in_sample_output = simulate_trades(candles[:split_idx], in_sample_signals, job.initial_capital, sizing, risk, costs)
                    oos_output = simulate_trades(candles[split_idx:], oos_signals, job.initial_capital, sizing, risk, costs)
                    metrics = compute_metrics(in_sample_output, job.initial_capital, job.timeframe)
                    out_of_sample_metrics = compute_metrics(oos_output, job.initial_capital, job.timeframe)

            monte_carlo = run_monte_carlo(output.trades, job.initial_capital) if job.run_monte_carlo else None

            db.add(
                BacktestResult(
                    job_id=job.id, metrics=metrics, out_of_sample_metrics=out_of_sample_metrics, monte_carlo=monte_carlo,
                    equity_curve=[[ts.isoformat(), equity] for ts, equity in output.equity_curve],
                )
            )
            for trade in output.trades:
                db.add(
                    BacktestTrade(
                        job_id=job.id, entry_ts=trade.entry_ts, entry_price=trade.entry_price, exit_ts=trade.exit_ts,
                        exit_price=trade.exit_price, quantity=trade.quantity, pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                        exit_reason=trade.exit_reason,
                    )
                )

            job.status = BacktestStatus.COMPLETED.value
            job.completed_at = datetime.now(timezone.utc)

            # A completed backtest is the real, earned trigger for the
            # DRAFT -> BACKTESTED transition modeled in state_machine.py
            # (PRD section 25) -- nothing before this point ever set it.
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
