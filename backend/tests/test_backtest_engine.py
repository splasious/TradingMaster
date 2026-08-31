import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.market_data import OhlcvCandle
from app.services.backtest.engine import CostConfig, PositionSizing, RiskRules, simulate_trades
from app.services.backtest.signals import BarSignals


def _candle(day_offset: int, open_, high, low, close, volume=1000.0) -> OhlcvCandle:
    return OhlcvCandle(
        instrument_id=uuid.uuid4(), timeframe="1d", ts=datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(days=day_offset),
        open=open_, high=high, low=low, close=close, volume=volume, source="test",
    )


NO_COSTS = CostConfig(brokerage_pct=0, slippage_pct=0, tax_pct=0)
FIXED_1 = PositionSizing(type="fixed_quantity", value=1)
NO_RISK = RiskRules()


def test_entry_signal_fills_at_next_bar_open_not_same_bar():
    candles = [
        _candle(0, 100, 101, 99, 100),  # signal fires here (close-based)
        _candle(1, 105, 106, 104, 105),  # must fill HERE at open=105
        _candle(2, 110, 111, 109, 110),
    ]
    signals = BarSignals(entry=[True, False, False], exit=[False, False, False])
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].entry_price == 105  # bar 1's open, not bar 0's close/open
    assert output.trades[0].entry_ts == candles[1].ts


def test_exit_signal_fills_at_next_bar_open():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 105, 106, 104, 105),  # entry fill here
        _candle(2, 110, 111, 109, 110),  # exit signal fires here (based on this bar's close)
        _candle(3, 115, 116, 114, 115),  # exit must fill HERE at open=115
    ]
    signals = BarSignals(entry=[True, False, False, False], exit=[False, False, True, False])
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].exit_price == 115
    assert output.trades[0].exit_ts == candles[3].ts


def test_stop_loss_triggers_intrabar_on_low():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # entry at open=100
        _candle(2, 100, 101, 90, 95),  # low=90 breaches a 5% stop (95)
    ]
    signals = BarSignals(entry=[True, False, False], exit=[False, False, False])
    risk = RiskRules(stop_loss_pct=5.0)
    output = simulate_trades(candles, signals, 100000, FIXED_1, risk, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].exit_reason == "stop_loss"
    assert output.trades[0].exit_price == pytest.approx(95.0)  # entry(100) * (1 - 5%)


def test_take_profit_triggers_intrabar_on_high():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # entry at open=100
        _candle(2, 100, 112, 99, 105),  # high=112 breaches a 10% target (110)
    ]
    signals = BarSignals(entry=[True, False, False], exit=[False, False, False])
    risk = RiskRules(take_profit_pct=10.0)
    output = simulate_trades(candles, signals, 100000, FIXED_1, risk, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].exit_reason == "take_profit"
    assert output.trades[0].exit_price == pytest.approx(110.0)


def test_open_position_closed_at_end_of_data():
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 101, 99, 100), _candle(2, 100, 105, 99, 103)]
    signals = BarSignals(entry=[True, False, False], exit=[False, False, False])
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].exit_reason == "end_of_data"
    assert output.trades[0].exit_price == candles[-1].close


def test_costs_reduce_pnl():
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 101, 99, 100), _candle(2, 100, 101, 99, 120)]
    signals = BarSignals(entry=[True, False, False], exit=[False, True, False])
    costs = CostConfig(brokerage_pct=1.0, slippage_pct=0, tax_pct=0)  # 1% each side
    output_with_costs = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, costs)
    output_no_costs = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert output_with_costs.trades[0].pnl < output_no_costs.trades[0].pnl


def test_slippage_worsens_entry_fill_price():
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 101, 99, 100)]
    signals = BarSignals(entry=[True, False], exit=[False, False])
    costs = CostConfig(brokerage_pct=0, slippage_pct=1.0, tax_pct=0)
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, costs)
    # position still open -> closed at end_of_data on last candle's close (100), entry price should be 101 (100*1.01)
    assert output.trades[0].entry_price == 100 * 1.01


def test_percent_capital_sizing_uses_available_cash():
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 101, 99, 100), _candle(2, 100, 101, 99, 100)]
    signals = BarSignals(entry=[True, False, False], exit=[False, False, False])
    sizing = PositionSizing(type="percent_capital", value=10.0)  # 10% of capital
    output = simulate_trades(candles, signals, 100000, sizing, NO_RISK, NO_COSTS)
    assert output.trades[0].quantity == 100  # (100000 * 10%) / 100 price = 100 units


def test_percent_capital_sizing_floors_to_whole_shares():
    # (100000 * 10%) / 137 = 72.99 -- must floor to 72 whole shares, never
    # a fraction, and never round up past what the allocation actually covers.
    candles = [_candle(0, 137, 138, 136, 137), _candle(1, 137, 138, 136, 137), _candle(2, 137, 138, 136, 137)]
    signals = BarSignals(entry=[True, False, False], exit=[False, False, False])
    sizing = PositionSizing(type="percent_capital", value=10.0)
    output = simulate_trades(candles, signals, 100000, sizing, NO_RISK, NO_COSTS)
    assert output.trades[0].quantity == 72
    assert output.trades[0].quantity == int(output.trades[0].quantity)


def test_no_signals_produces_no_trades():
    candles = [_candle(i, 100, 101, 99, 100) for i in range(5)]
    signals = BarSignals(entry=[False] * 5, exit=[False] * 5)
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)
    assert output.trades == []
    assert output.final_equity == 100000


def test_cannot_afford_entry_skips_trade():
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 1000, 1001, 999, 1000)]
    signals = BarSignals(entry=[True, False], exit=[False, False])
    sizing = PositionSizing(type="fixed_quantity", value=1_000_000)  # way more than capital can afford
    output = simulate_trades(candles, signals, 100000, sizing, NO_RISK, NO_COSTS)
    assert output.trades == []
    assert output.final_equity == 100000


def test_equity_curve_has_one_point_per_candle():
    candles = [_candle(i, 100, 101, 99, 100) for i in range(7)]
    signals = BarSignals(entry=[False] * 7, exit=[False] * 7)
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)
    assert len(output.equity_curve) == 7
