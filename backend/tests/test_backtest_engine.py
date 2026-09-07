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


# ---------------------------------------------------------------- shorts --


def _no_signal(n: int) -> list[bool]:
    return [False] * n


def test_short_entry_fills_at_next_bar_open_not_same_bar():
    candles = [
        _candle(0, 100, 101, 99, 100),  # SHORT signal fires here (close-based)
        _candle(1, 105, 106, 104, 105),  # must fill HERE at open=105
        _candle(2, 110, 111, 109, 110),
    ]
    n = len(candles)
    signals = BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True, False, False], short_exit=_no_signal(n))
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "short"
    assert output.trades[0].entry_price == 105
    assert output.trades[0].entry_ts == candles[1].ts


def test_cover_signal_fills_at_next_bar_open():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 105, 106, 104, 105),  # short entry fill here
        _candle(2, 95, 96, 94, 95),  # COVER signal fires here (based on this bar's close)
        _candle(3, 90, 91, 89, 90),  # cover must fill HERE at open=90
    ]
    n = len(candles)
    signals = BarSignals(
        entry=_no_signal(n), exit=_no_signal(n),
        short_entry=[True, False, False, False], short_exit=[False, False, True, False],
    )
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].exit_price == 90
    assert output.trades[0].exit_ts == candles[3].ts
    assert output.trades[0].pnl > 0  # covered lower than entry -> profit on a short


def test_short_stop_loss_triggers_intrabar_on_high():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # short entry at open=100
        _candle(2, 100, 105, 99, 103),  # high=105 breaches a 5% stop (105)
    ]
    n = len(candles)
    signals = BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True, False, False], short_exit=_no_signal(n))
    risk = RiskRules(stop_loss_pct=5.0)
    output = simulate_trades(candles, signals, 100000, FIXED_1, risk, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "short"
    assert output.trades[0].exit_reason == "stop_loss"
    assert output.trades[0].exit_price == pytest.approx(105.0)  # entry(100) * (1 + 5%)
    assert output.trades[0].pnl < 0  # stopped out higher than entry -> loss on a short


def test_short_take_profit_triggers_intrabar_on_low():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # short entry at open=100
        _candle(2, 100, 101, 88, 95),  # low=88 breaches a 10% target (90)
    ]
    n = len(candles)
    signals = BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True, False, False], short_exit=_no_signal(n))
    risk = RiskRules(take_profit_pct=10.0)
    output = simulate_trades(candles, signals, 100000, FIXED_1, risk, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].exit_reason == "take_profit"
    assert output.trades[0].exit_price == pytest.approx(90.0)
    assert output.trades[0].pnl > 0


def test_open_short_closed_at_end_of_data():
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 101, 99, 100), _candle(2, 100, 105, 95, 97)]
    n = len(candles)
    signals = BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True, False, False], short_exit=_no_signal(n))
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "short"
    assert output.trades[0].exit_reason == "end_of_data"
    assert output.trades[0].exit_price == candles[-1].close


def test_short_slippage_worsens_entry_and_exit_fills():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # short entry here
        _candle(2, 100, 101, 99, 100),  # cover signal fires here
        _candle(3, 100, 101, 99, 100),  # cover fills here
    ]
    n = len(candles)
    signals = BarSignals(
        entry=_no_signal(n), exit=_no_signal(n),
        short_entry=[True, False, False, False], short_exit=[False, False, True, False],
    )
    costs = CostConfig(brokerage_pct=0, slippage_pct=1.0, tax_pct=0)
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, costs)

    # Opening a short is a sale -- adverse slippage moves the fill DOWN.
    assert output.trades[0].entry_price == pytest.approx(100 * 0.99)
    # Covering is a purchase -- adverse slippage moves the fill UP.
    assert output.trades[0].exit_price == pytest.approx(100 * 1.01)


def test_short_signal_while_long_closes_the_long_via_automatic_reversal():
    """A strategy that only ever emits BUY/SHORT (never an explicit SELL)
    still gets its long closed when the bearish setup fires -- the
    opposite-direction signal is the exit condition, since generate_signal
    has no way to know it's currently long."""
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # long entry fills here
        _candle(2, 100, 101, 99, 90),  # SHORT signal fires here (close-based)
        _candle(3, 85, 86, 84, 85),  # long must close HERE at open=85 -- no explicit SELL ever fired
    ]
    n = len(candles)
    signals = BarSignals(entry=[True, False, False, False], exit=_no_signal(n), short_entry=[False, False, True, False], short_exit=_no_signal(n))
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "long"
    assert output.trades[0].exit_price == 85
    assert output.trades[0].exit_ts == candles[3].ts


def test_buy_signal_while_short_covers_via_automatic_reversal():
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # short entry fills here
        _candle(2, 100, 101, 99, 110),  # BUY signal fires here (close-based) -- no explicit COVER ever fired
        _candle(3, 115, 116, 114, 115),  # short must cover HERE at open=115
    ]
    n = len(candles)
    signals = BarSignals(entry=[False, False, True, False], exit=_no_signal(n), short_entry=[True, False, False, False], short_exit=_no_signal(n))
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "short"
    assert output.trades[0].exit_price == 115
    assert output.trades[0].exit_ts == candles[3].ts


def test_cannot_open_long_and_short_at_once():
    """A BUY and a SHORT signal on the same flat bar can't both win --
    long entry is checked first, so it takes the slot and the short signal
    is simply dropped for that bar (no crash, no split position)."""
    candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 101, 99, 100)]
    signals = BarSignals(entry=[True, False], exit=[False, False], short_entry=[True, False], short_exit=[False, False])
    output = simulate_trades(candles, signals, 100000, FIXED_1, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "long"
