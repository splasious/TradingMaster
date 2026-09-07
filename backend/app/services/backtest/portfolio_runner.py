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
    compute_python_signals,
    compute_visual_signals,
)
from app.services.strategy.state_machine import StrategyStatus, can_transition

MAX_CANDLES = 3000  # per instrument -- bounds worst-case runtime of the O(n^2) visual-mode signal computation
MIN_CANDLES = 30


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

            instruments = []
            candles_by_instrument: dict[str, list] = {}
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

            for raw_id in job.instrument_ids:
                instrument = await db.get(Instrument, uuid.UUID(raw_id))
                if instrument is None:
                    continue
                candles = (await load_candles(db, instrument.id, job.timeframe, job.start_date, job.end_date))[-MAX_CANDLES:]
                if len(candles) < MIN_CANDLES:
                    skipped_symbols.append(instrument.symbol)
                    continue
                try:
                    if version.python_code:
                        signals = await compute_python_signals(candles, version.python_code, version.parameters)
                    else:
                        signals = compute_visual_signals(candles, version.entry_rules, version.exit_rules)
                except SignalComputationError as exc:
                    last_signal_error = str(exc)
                    skipped_symbols.append(instrument.symbol)
                    continue

                inst_id = str(instrument.id)
                instruments.append(instrument)
                candles_by_instrument[inst_id] = candles
                signals_by_instrument[inst_id] = signals

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
                instruments, candles_by_instrument, signals_by_instrument, job.initial_capital, sizing, risk, costs
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
                        bars_held=trade.bars_held, exit_reason=trade.exit_reason, status=trade.status,
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
