"""KPIs for an Advanced (native) backtest -- agreed 6 Oct (step 1).

Pure: works from what native_runner.py collects -- one equity value per
trading day (cash plus every open position at that day's close, less the
charges paid so far), the closed trades with their estimated charges, and
NIFTY 50's daily candles over the same dates. The simple-rule backtests'
metrics.py works from its own engine's output; the formulas match it where
both have the number (Sharpe/Sortino annualised over 252 trading days, 0%
risk-free).
"""

import math
import statistics
from datetime import date, datetime
from typing import Any

from app.services.paper_trading.trade_record import CHARGE_PARTS

KPI_VERSION = 2
TRADING_DAYS_PER_YEAR = 252
MIN_DAYS_FOR_CAGR = 90


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None or math.isnan(value) or math.isinf(value) else round(value, digits)


def _drawdown(points: list[tuple[date, float]]) -> dict[str, Any]:
    """Deepest fall from a running peak (% and amount, with its peak and
    lowest dates, and whether a later value got back to that peak) and the
    longest stretch below a previous peak (calendar days)."""
    peak_value, peak_day = points[0][1], points[0][0]
    worst: tuple[float, float, date, date, float] | None = None  # pct, amount, peak day, trough day, peak value
    longest, under_since = 0, None
    for day, value in points:
        if value >= peak_value:
            if under_since is not None:
                longest = max(longest, (day - under_since).days)
                under_since = None
            peak_value, peak_day = value, day
            continue
        if under_since is None:
            under_since = peak_day
        pct = (peak_value - value) / peak_value * 100 if peak_value > 0 else 0.0
        if worst is None or pct > worst[0]:
            worst = (pct, peak_value - value, peak_day, day, peak_value)
    if under_since is not None:
        longest = max(longest, (points[-1][0] - under_since).days)
    return {
        "max_drawdown_pct": _r(worst[0]) if worst else 0.0, "max_drawdown_amount": _r(worst[1]) if worst else 0.0,
        "drawdown_peak_date": worst[2].isoformat() if worst else None, "drawdown_trough_date": worst[3].isoformat() if worst else None,
        "max_drawdown_recovered": worst is None or any(value >= worst[4] for day, value in points if day > worst[3]),
        "longest_drawdown_days": longest, "in_drawdown_at_end": under_since is not None,
    }


def _period_returns(points: list[tuple[date, float]], initial: float, key) -> list[tuple[Any, float]]:
    """Return per period (key(day)), from the previous period's last value."""
    out: list[tuple[Any, float]] = []
    previous, current_key, last = initial, None, initial
    for day, value in points:
        k = key(day)
        if current_key is not None and k != current_key:
            out.append((current_key, (last / previous - 1) * 100 if previous else 0.0))
            previous = last
        current_key, last = k, value
    if current_key is not None:
        out.append((current_key, (last / previous - 1) * 100 if previous else 0.0))
    return out


def _streaks(results: list[bool]) -> tuple[int, int]:
    best_win = best_loss = run = 0
    last = None
    for won in results:
        run = run + 1 if won == last else 1
        last = won
        if won:
            best_win = max(best_win, run)
        else:
            best_loss = max(best_loss, run)
    return best_win, best_loss


def compute_native_metrics(
    *, initial_capital: float, daily: list[tuple[date, float, bool]], trades: list[dict],
    benchmark: list[tuple[date, float, float]] | None = None, benchmark_symbol: str = "NIFTY 50",
) -> dict[str, Any]:
    """`daily`: (trading day, equity at its close net of charges paid, in the
    market that day). `trades`: closed trades with opened_at, closed_at, pnl
    (gross) and charges / charges_breakdown (None when not estimable).
    `benchmark`: (day, open, close) of NIFTY 50's daily candles."""
    trades = sorted(trades, key=lambda t: t["closed_at"])
    points = [(day, value) for day, value, _ in daily]
    final = points[-1][1] if points else initial_capital

    # --- Costs ----------------------------------------------------------
    gross = sum(t["pnl"] for t in trades)
    charges = sum(t.get("charges") or 0.0 for t in trades)
    breakdown = dict.fromkeys(CHARGE_PARTS, 0.0)
    for t in trades:
        for part, value in (t.get("charges_breakdown") or {}).items():
            if part in breakdown:
                breakdown[part] += value
    net_trades = [t["pnl"] - (t.get("charges") or 0.0) for t in trades]

    # --- Returns and risk from the daily equity ---------------------------
    series = [initial_capital] + [value for _, value in points]
    daily_returns = [(b / a - 1) for a, b in zip(series, series[1:]) if a]
    days = (points[-1][0] - points[0][0]).days + 1 if points else 0
    total_return = (final / initial_capital - 1) * 100 if initial_capital else 0.0
    cagr = ((final / initial_capital) ** (365.25 / days) - 1) * 100 if days >= MIN_DAYS_FOR_CAGR and final > 0 and initial_capital else None
    sharpe = sortino = volatility = None
    if len(daily_returns) >= 2:
        mean, stdev = statistics.mean(daily_returns), statistics.stdev(daily_returns)
        volatility = stdev * math.sqrt(TRADING_DAYS_PER_YEAR) * 100
        if stdev > 0:
            sharpe = mean / stdev * math.sqrt(TRADING_DAYS_PER_YEAR)
        downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in daily_returns) / len(daily_returns))
        if downside > 0:
            sortino = mean / downside * math.sqrt(TRADING_DAYS_PER_YEAR)
    drawdown = _drawdown([(points[0][0], initial_capital)] + points) if points else _drawdown([(date.min, initial_capital)])
    calmar = cagr / drawdown["max_drawdown_pct"] if cagr is not None and drawdown["max_drawdown_pct"] else None

    # --- Trade quality (after charges) ------------------------------------
    wins = [p for p in net_trades if p > 0]
    losses = [p for p in net_trades if p <= 0]
    avg_win = statistics.mean(wins) if wins else None
    avg_loss = statistics.mean(losses) if losses else None
    loss_sum = -sum(losses)
    max_wins, max_losses = _streaks([p > 0 for p in net_trades])
    holding = [
        (t["closed_at"] - t["opened_at"]).total_seconds() / 3600 for t in trades
        if isinstance(t["closed_at"], datetime) and isinstance(t["opened_at"], datetime)
    ]

    # --- NIFTY 50 over the same dates -------------------------------------
    bench = None
    bench_curve: list[list] = []
    if benchmark and points:
        first_open = benchmark[0][1] or benchmark[0][2]
        closes = [(day, close) for day, _, close in benchmark]
        bench_return = (closes[-1][1] / first_open - 1) * 100 if first_open else None
        bench_dd = _drawdown([(benchmark[0][0], first_open)] + closes)["max_drawdown_pct"]
        bench = {
            "symbol": benchmark_symbol, "return_pct": _r(bench_return), "max_drawdown_pct": bench_dd,
            "excess_return_pct": _r(total_return - bench_return) if bench_return is not None else None,
        }
        bench_curve = [[day.isoformat(), round(initial_capital * close / first_open, 2)] for day, close in closes]

    in_market = sum(1 for _, _, holding_any in daily if holding_any)
    return {
        "kpi_version": KPI_VERSION,
        # Returns
        "initial_capital": _r(initial_capital), "final_capital": _r(final),
        "net_pnl": _r(final - initial_capital), "gross_pnl": _r(gross), "total_return_pct": _r(total_return), "cagr_pct": _r(cagr),
        # Risk
        **drawdown, "annual_volatility_pct": _r(volatility),
        "sharpe_ratio": _r(sharpe, 3), "sortino_ratio": _r(sortino, 3), "calmar_ratio": _r(calmar, 3),
        # Trade quality, after charges
        "trade_count": len(trades), "win_rate_pct": _r(len(wins) / len(trades) * 100 if trades else 0.0),
        "profit_factor": _r(sum(wins) / loss_sum, 3) if loss_sum > 0 else None, "no_losing_trades": bool(trades) and not losses,
        "avg_win": _r(avg_win), "avg_loss": _r(avg_loss),
        "payoff_ratio": _r(avg_win / -avg_loss, 3) if avg_win is not None and avg_loss else None,
        "expectancy": _r(statistics.mean(net_trades)) if net_trades else None,
        "best_trade": _r(max(net_trades)) if net_trades else None, "worst_trade": _r(min(net_trades)) if net_trades else None,
        "max_consecutive_wins": max_wins, "max_consecutive_losses": max_losses,
        "avg_holding_hours": _r(statistics.mean(holding), 1) if holding else None,
        # Costs
        "charges_total": _r(charges), "charges_breakdown": {k: _r(v) for k, v in breakdown.items()},
        "charges_pct_of_gross": _r(charges / gross * 100) if gross > 0 else None,
        "trades_without_charge_estimate": sum(1 for t in trades if t.get("charges") is None),
        # Consistency
        "trading_days": len(daily), "exposure_pct": _r(in_market / len(daily) * 100 if daily else 0.0),
        "monthly_returns": [{"year": y, "month": m, "return_pct": _r(v)} for (y, m), v in _period_returns(points, initial_capital, lambda d: (d.year, d.month))],
        "yearly_returns": [{"year": y, "return_pct": _r(v)} for y, v in _period_returns(points, initial_capital, lambda d: d.year)],
        # NIFTY 50
        "benchmark": bench, "benchmark_curve": bench_curve,
    }
