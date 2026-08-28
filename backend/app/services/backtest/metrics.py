"""Standard backtest performance metrics (PRD section 19), all computed
from the trade list and equity curve `simulate_trades` produces -- no
metric here is estimated or hard-coded, each is the real formula applied
to the actual simulated series.
"""

import math
from datetime import datetime
from typing import TypedDict

from app.services.backtest.engine import BacktestOutput, Trade

TRADING_PERIODS_PER_YEAR = {"1d": 252, "1wk": 52, "1mo": 12}


class Metrics(TypedDict):
    net_profit: float
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    avg_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    profit_factor: float
    win_rate_pct: float
    loss_rate_pct: float
    avg_win: float
    avg_loss: float
    expectancy: float
    num_trades: int
    avg_holding_period_days: float
    best_trade: float
    worst_trade: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    recovery_factor: float


def _drawdown_series(equity_curve: list[tuple[datetime, float]]) -> list[float]:
    if not equity_curve:
        return []
    peak = equity_curve[0][1]
    drawdowns = []
    for _, equity in equity_curve:
        peak = max(peak, equity)
        drawdowns.append((equity - peak) / peak * 100 if peak > 0 else 0.0)
    return drawdowns


def _max_streak(values: list[bool]) -> int:
    best = current = 0
    for v in values:
        current = current + 1 if v else 0
        best = max(best, current)
    return best


def compute_metrics(output: BacktestOutput, initial_capital: float, timeframe: str = "1d") -> Metrics:
    trades: list[Trade] = output.trades
    equity_curve = output.equity_curve

    net_profit = output.final_equity - initial_capital
    total_return_pct = (net_profit / initial_capital * 100) if initial_capital else 0.0

    cagr_pct = 0.0
    if equity_curve and initial_capital > 0:
        days = max(1, (equity_curve[-1][0] - equity_curve[0][0]).days)
        years = days / 365.25
        if years > 0 and output.final_equity > 0:
            cagr_pct = ((output.final_equity / initial_capital) ** (1 / years) - 1) * 100

    drawdowns = _drawdown_series(equity_curve)
    max_drawdown_pct = abs(min(drawdowns)) if drawdowns else 0.0
    negative_dd = [d for d in drawdowns if d < 0]
    avg_drawdown_pct = abs(sum(negative_dd) / len(negative_dd)) if negative_dd else 0.0

    periods_per_year = TRADING_PERIODS_PER_YEAR.get(timeframe, 252)
    returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1][1]
        if prev > 0:
            returns.append((equity_curve[i][1] - prev) / prev)
    sharpe_ratio = 0.0
    sortino_ratio = 0.0
    if returns:
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std = math.sqrt(variance)
        if std > 0:
            sharpe_ratio = mean_return / std * math.sqrt(periods_per_year)
        downside = [r for r in returns if r < 0]
        if downside:
            downside_std = math.sqrt(sum(r**2 for r in downside) / len(downside))
            if downside_std > 0:
                sortino_ratio = mean_return / downside_std * math.sqrt(periods_per_year)

    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    win_rate_pct = (len(wins) / len(trades) * 100) if trades else 0.0
    loss_rate_pct = (len(losses) / len(trades) * 100) if trades else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = sum(t.pnl for t in trades) / len(trades) if trades else 0.0

    holding_periods = [(t.exit_ts - t.entry_ts).total_seconds() / 86400 for t in trades]
    avg_holding_period_days = sum(holding_periods) / len(holding_periods) if holding_periods else 0.0

    best_trade = max((t.pnl for t in trades), default=0.0)
    worst_trade = min((t.pnl for t in trades), default=0.0)
    max_consecutive_wins = _max_streak([t.pnl > 0 for t in trades])
    max_consecutive_losses = _max_streak([t.pnl <= 0 for t in trades])
    recovery_factor = (net_profit / (max_drawdown_pct / 100 * initial_capital)) if max_drawdown_pct > 0 else 0.0

    return Metrics(
        net_profit=round(net_profit, 2), total_return_pct=round(total_return_pct, 2), cagr_pct=round(cagr_pct, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2), avg_drawdown_pct=round(avg_drawdown_pct, 2),
        sharpe_ratio=round(sharpe_ratio, 3), sortino_ratio=round(sortino_ratio, 3),
        profit_factor=round(profit_factor, 3) if profit_factor != float("inf") else 999.0,
        win_rate_pct=round(win_rate_pct, 2), loss_rate_pct=round(loss_rate_pct, 2), avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2), expectancy=round(expectancy, 2), num_trades=len(trades),
        avg_holding_period_days=round(avg_holding_period_days, 2), best_trade=round(best_trade, 2),
        worst_trade=round(worst_trade, 2), max_consecutive_wins=max_consecutive_wins,
        max_consecutive_losses=max_consecutive_losses, recovery_factor=round(recovery_factor, 3),
    )
