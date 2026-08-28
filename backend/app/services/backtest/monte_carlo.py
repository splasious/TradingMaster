"""Trade-resampling Monte Carlo (PRD section 18): bootstraps the actual
realized trade P&Ls into many alternate equity paths to show how much of
the backtest's result could be luck-of-the-sequencing rather than edge.
Does not simulate new trades -- only reorders/resamples the ones that
already happened.
"""

import random
import statistics
from typing import TypedDict

from app.services.backtest.engine import Trade


class MonteCarloResult(TypedDict):
    simulations: int
    final_equity_p5: float
    final_equity_p50: float
    final_equity_p95: float
    max_drawdown_pct_p50: float
    max_drawdown_pct_p95: float
    probability_of_profit_pct: float


def _max_drawdown_pct(equity_path: list[float]) -> float:
    peak = equity_path[0] if equity_path else 0.0
    max_dd = 0.0
    for equity in equity_path:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, (equity - peak) / peak * 100)
    return abs(max_dd)


def run_monte_carlo(trades: list[Trade], initial_capital: float, simulations: int = 1000, seed: int | None = None) -> MonteCarloResult:
    if not trades:
        return MonteCarloResult(
            simulations=0, final_equity_p5=initial_capital, final_equity_p50=initial_capital,
            final_equity_p95=initial_capital, max_drawdown_pct_p50=0.0, max_drawdown_pct_p95=0.0,
            probability_of_profit_pct=0.0,
        )

    rng = random.Random(seed)
    pnls = [t.pnl for t in trades]
    n = len(pnls)

    final_equities: list[float] = []
    max_drawdowns: list[float] = []
    for _ in range(simulations):
        resampled = [rng.choice(pnls) for _ in range(n)]
        equity = initial_capital
        path = [equity]
        for pnl in resampled:
            equity += pnl
            path.append(equity)
        final_equities.append(equity)
        max_drawdowns.append(_max_drawdown_pct(path))

    final_equities.sort()
    max_drawdowns.sort()

    def pct(sorted_values: list[float], p: float) -> float:
        idx = min(len(sorted_values) - 1, max(0, int(p * len(sorted_values))))
        return sorted_values[idx]

    profitable = sum(1 for e in final_equities if e > initial_capital)

    return MonteCarloResult(
        simulations=simulations,
        final_equity_p5=round(pct(final_equities, 0.05), 2),
        final_equity_p50=round(statistics.median(final_equities), 2),
        final_equity_p95=round(pct(final_equities, 0.95), 2),
        max_drawdown_pct_p50=round(statistics.median(max_drawdowns), 2),
        max_drawdown_pct_p95=round(pct(max_drawdowns, 0.95), 2),
        probability_of_profit_pct=round(profitable / simulations * 100, 2),
    )
