from datetime import datetime, timedelta, timezone

from app.services.backtest.engine import Trade
from app.services.backtest.monte_carlo import run_monte_carlo

BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _trade(pnl: float) -> Trade:
    return Trade(entry_ts=BASE, entry_price=100, exit_ts=BASE + timedelta(days=1), exit_price=100 + pnl, quantity=1, pnl=pnl, pnl_pct=pnl, exit_reason="signal")


def test_monte_carlo_all_winning_trades_always_profitable():
    trades = [_trade(100), _trade(200), _trade(150)]
    result = run_monte_carlo(trades, 100000, simulations=200, seed=42)
    assert result["probability_of_profit_pct"] == 100.0
    assert result["final_equity_p5"] > 100000


def test_monte_carlo_all_losing_trades_never_profitable():
    trades = [_trade(-100), _trade(-200), _trade(-150)]
    result = run_monte_carlo(trades, 100000, simulations=200, seed=42)
    assert result["probability_of_profit_pct"] == 0.0
    assert result["final_equity_p95"] < 100000


def test_monte_carlo_deterministic_with_seed():
    trades = [_trade(100), _trade(-50), _trade(75)]
    r1 = run_monte_carlo(trades, 100000, simulations=100, seed=7)
    r2 = run_monte_carlo(trades, 100000, simulations=100, seed=7)
    assert r1 == r2


def test_monte_carlo_empty_trades_does_not_crash():
    result = run_monte_carlo([], 100000, simulations=100)
    assert result["simulations"] == 0
    assert result["final_equity_p50"] == 100000


def test_monte_carlo_percentile_ordering():
    trades = [_trade(100), _trade(-300), _trade(50), _trade(-20), _trade(400)]
    result = run_monte_carlo(trades, 100000, simulations=500, seed=1)
    assert result["final_equity_p5"] <= result["final_equity_p50"] <= result["final_equity_p95"]
