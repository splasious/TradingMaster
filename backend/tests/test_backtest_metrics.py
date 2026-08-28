from datetime import datetime, timedelta, timezone

import pytest

from app.services.backtest.engine import BacktestOutput, Trade
from app.services.backtest.metrics import compute_metrics

BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _trade(days_offset: int, pnl: float, pnl_pct: float, reason="signal") -> Trade:
    return Trade(
        entry_ts=BASE + timedelta(days=days_offset), entry_price=100, exit_ts=BASE + timedelta(days=days_offset + 1),
        exit_price=100 + pnl, quantity=1, pnl=pnl, pnl_pct=pnl_pct, exit_reason=reason,
    )


def _equity_curve(values: list[float]):
    return [(BASE + timedelta(days=i), v) for i, v in enumerate(values)]


def test_net_profit_and_total_return():
    output = BacktestOutput(trades=[], equity_curve=_equity_curve([100000, 105000, 110000]), final_equity=110000)
    metrics = compute_metrics(output, 100000)
    assert metrics["net_profit"] == 10000
    assert metrics["total_return_pct"] == 10.0


def test_win_rate_and_profit_factor():
    trades = [_trade(0, 100, 1), _trade(1, 200, 2), _trade(2, -50, -0.5)]
    output = BacktestOutput(trades=trades, equity_curve=_equity_curve([100000] * 4), final_equity=100250)
    metrics = compute_metrics(output, 100000)
    assert metrics["num_trades"] == 3
    assert metrics["win_rate_pct"] == round(2 / 3 * 100, 2)
    assert metrics["profit_factor"] == round(300 / 50, 3)


def test_all_losing_trades_zero_profit_factor_not_crash():
    trades = [_trade(0, -10, -0.1), _trade(1, -20, -0.2)]
    output = BacktestOutput(trades=trades, equity_curve=_equity_curve([100000, 99990, 99970]), final_equity=99970)
    metrics = compute_metrics(output, 100000)
    assert metrics["profit_factor"] == 0.0
    assert metrics["win_rate_pct"] == 0.0


def test_no_trades_does_not_crash():
    output = BacktestOutput(trades=[], equity_curve=_equity_curve([100000, 100000]), final_equity=100000)
    metrics = compute_metrics(output, 100000)
    assert metrics["num_trades"] == 0
    assert metrics["win_rate_pct"] == 0.0
    assert metrics["sharpe_ratio"] == 0.0


def test_max_drawdown_from_known_equity_curve():
    # peak 100 -> trough 80 -> recovery 90: drawdown = (80-100)/100 = -20%
    output = BacktestOutput(trades=[], equity_curve=_equity_curve([100, 110, 100, 80, 90]), final_equity=90)
    metrics = compute_metrics(output, 100)
    assert metrics["max_drawdown_pct"] == round(abs((80 - 110) / 110 * 100), 2)


def test_consecutive_wins_and_losses():
    trades = [_trade(0, 10, 1), _trade(1, 10, 1), _trade(2, -5, -0.5), _trade(3, -5, -0.5), _trade(4, -5, -0.5), _trade(5, 10, 1)]
    output = BacktestOutput(trades=trades, equity_curve=_equity_curve([100000] * 7), final_equity=100025)
    metrics = compute_metrics(output, 100000)
    assert metrics["max_consecutive_wins"] == 2
    assert metrics["max_consecutive_losses"] == 3


def test_best_and_worst_trade():
    trades = [_trade(0, 50, 0.5), _trade(1, -200, -2), _trade(2, 300, 3)]
    output = BacktestOutput(trades=trades, equity_curve=_equity_curve([100000] * 4), final_equity=100150)
    metrics = compute_metrics(output, 100000)
    assert metrics["best_trade"] == 300
    assert metrics["worst_trade"] == -200


def test_cagr_positive_for_growing_equity_over_a_year():
    equity_curve = [(BASE, 100000), (BASE + timedelta(days=365), 110000)]
    output = BacktestOutput(trades=[], equity_curve=equity_curve, final_equity=110000)
    metrics = compute_metrics(output, 100000)
    assert metrics["cagr_pct"] == pytest.approx(10.0, abs=0.5)
