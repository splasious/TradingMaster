"""Advanced backtest KPIs (backtest/native_metrics.py), checked by hand."""

import math
import statistics
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.backtest.native_metrics import compute_native_metrics

D = [date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4)]
EQUITY = [110.0, 99.0, 105.0, 120.0]  # from 100: up 10%, a 10% fall from the 110 peak, then a new high


def _t(day: int, hours: float, pnl: float, charges: float | None, parts: dict | None = None) -> dict:
    opened = datetime(2026, 2, day, 4, 0, tzinfo=timezone.utc)
    return {"opened_at": opened, "closed_at": opened + timedelta(hours=hours), "pnl": pnl, "charges": charges, "charges_breakdown": parts}


TRADES = [
    _t(2, 2, 100.0, 10.0, {"brokerage": 4.0, "stt": 6.0}),
    _t(3, 4, -50.0, 5.0, {"brokerage": 5.0}),
    _t(4, 6, 30.0, None),  # no estimate for this one
]


def _metrics(**kwargs):
    defaults = {"initial_capital": 100.0, "daily": [(d, e, i != 0) for i, (d, e) in enumerate(zip(D, EQUITY))], "trades": TRADES,
                "benchmark": [(D[0], 100.0, 102.0), (D[1], 101.0, 96.9), (D[2], 97.0, 99.96), (D[3], 100.0, 112.2)]}
    return compute_native_metrics(**{**defaults, **kwargs})


def test_returns_and_the_daily_risk_numbers():
    m = _metrics()
    assert (m["kpi_version"], m["final_capital"], m["net_pnl"], m["total_return_pct"]) == (2, 120.0, 20.0, 20.0)
    assert m["cagr_pct"] is None  # 6 days: too short to annualise
    returns = [0.10, 99 / 110 - 1, 105 / 99 - 1, 120 / 105 - 1]
    mean, stdev = statistics.mean(returns), statistics.stdev(returns)
    assert m["sharpe_ratio"] == round(mean / stdev * math.sqrt(252), 3)
    downside = math.sqrt(((99 / 110 - 1) ** 2) / 4)
    assert m["sortino_ratio"] == round(mean / downside * math.sqrt(252), 3)
    assert m["annual_volatility_pct"] == round(stdev * math.sqrt(252) * 100, 2)


def test_drawdown_depth_dates_and_recovery():
    m = _metrics()
    assert (m["max_drawdown_pct"], m["max_drawdown_amount"]) == (10.0, 11.0)
    assert (m["drawdown_peak_date"], m["drawdown_trough_date"]) == ("2026-01-30", "2026-02-02")
    assert m["max_drawdown_recovered"] is True and m["in_drawdown_at_end"] is False
    assert m["longest_drawdown_days"] == 5  # 30 Jan peak, back above it on 4 Feb

    falling = _metrics(daily=[(D[0], 110.0, True), (D[1], 99.0, True), (D[2], 104.0, True)])
    assert falling["max_drawdown_recovered"] is False and falling["in_drawdown_at_end"] is True
    assert falling["longest_drawdown_days"] == 4


def test_trade_quality_is_after_charges():
    m = _metrics()  # net: +90, -55, +30
    assert (m["trade_count"], m["win_rate_pct"]) == (3, 66.67)
    assert m["profit_factor"] == round(120 / 55, 3)
    assert (m["avg_win"], m["avg_loss"], m["payoff_ratio"]) == (60.0, -55.0, round(60 / 55, 3))
    assert m["expectancy"] == round(65 / 3, 2)
    assert (m["best_trade"], m["worst_trade"]) == (90.0, -55.0)
    assert (m["max_consecutive_wins"], m["max_consecutive_losses"]) == (1, 1)
    assert m["avg_holding_hours"] == 4.0
    assert (m["gross_pnl"], m["charges_total"], m["trades_without_charge_estimate"]) == (80.0, 15.0, 1)
    assert m["charges_breakdown"]["brokerage"] == 9.0 and m["charges_breakdown"]["stt"] == 6.0
    assert m["charges_pct_of_gross"] == round(15 / 80 * 100, 2)


def test_months_years_exposure_and_nifty():
    m = _metrics()
    assert m["monthly_returns"] == [{"year": 2026, "month": 1, "return_pct": 10.0},
                                    {"year": 2026, "month": 2, "return_pct": round((120 / 110 - 1) * 100, 2)}]
    assert m["yearly_returns"] == [{"year": 2026, "return_pct": 20.0}]
    assert (m["trading_days"], m["exposure_pct"]) == (4, 75.0)
    bench = m["benchmark"]
    assert bench["return_pct"] == pytest.approx(12.2) and bench["excess_return_pct"] == pytest.approx(7.8)
    assert bench["max_drawdown_pct"] == 5.0  # 102 -> 96.9
    assert m["benchmark_curve"][0] == ["2026-01-30", 102.0] and m["benchmark_curve"][-1] == ["2026-02-04", 112.2]


def test_cagr_and_calmar_over_a_year_and_no_losers():
    year = [(date(2025, 1, 1) + timedelta(days=i), 100 + i * 0.1, True) for i in range(365)]
    m = _metrics(daily=year, trades=[_t(2, 1, 5.0, 0.0)], benchmark=None)
    assert m["cagr_pct"] == pytest.approx(((year[-1][1] / 100) ** (365.25 / 365) - 1) * 100, abs=0.01)
    assert m["max_drawdown_pct"] == 0.0 and m["calmar_ratio"] is None
    assert m["profit_factor"] is None and m["no_losing_trades"] is True and m["benchmark"] is None
