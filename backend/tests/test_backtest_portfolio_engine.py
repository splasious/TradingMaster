import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.market_data import OhlcvCandle
from app.services.backtest.engine import CostConfig, RiskRules
from app.services.backtest.metrics import compute_metrics
from app.services.backtest.portfolio_engine import PortfolioSizing, as_metrics_input, simulate_portfolio
from app.services.backtest.signals import BarSignals


class FakeInstrument:
    def __init__(self, symbol: str):
        self.id = uuid.uuid4()
        self.symbol = symbol


def _candle(day_offset: int, open_, high, low, close, volume=1000.0) -> OhlcvCandle:
    return OhlcvCandle(
        instrument_id=uuid.uuid4(), timeframe="1d", ts=datetime(2026, 1, 5, tzinfo=timezone.utc) + timedelta(days=day_offset),
        open=open_, high=high, low=low, close=close, volume=volume, source="test",
    )


NO_COSTS = CostConfig(brokerage_pct=0, slippage_pct=0, tax_pct=0)
NO_RISK = RiskRules()
DEFAULT_SIZING = PortfolioSizing(position_size_pct=50.0, max_open_positions=10)


def test_two_instruments_hold_concurrent_positions_from_one_capital_pool():
    a, b = FakeInstrument("AAA"), FakeInstrument("BBB")
    candles = [_candle(i, 100, 101, 99, 100) for i in range(5)]
    entry = [True, False, False, False, False]
    no_exit = [False] * 5
    signals = {str(a.id): BarSignals(entry=entry, exit=no_exit), str(b.id): BarSignals(entry=entry, exit=no_exit)}
    candles_by = {str(a.id): candles, str(b.id): candles}

    output = simulate_portfolio([a, b], candles_by, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    # Both fire the same signal, both should get filled from the SAME pool --
    # proof this is one shared portfolio, not two independent full-capital runs.
    assert len(output.trades) == 2
    symbols = {t.symbol for t in output.trades}
    assert symbols == {"AAA", "BBB"}


def test_position_size_pct_caps_allocation_per_instrument():
    a = FakeInstrument("AAA")
    candles = [_candle(i, 100, 101, 99, 100) for i in range(3)]
    signals = {str(a.id): BarSignals(entry=[True, False, False], exit=[False, False, False])}
    sizing = PortfolioSizing(position_size_pct=10.0, max_open_positions=10)

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, sizing, NO_RISK, NO_COSTS)

    # 10% of 100000 equity / 100 price = 100 shares
    assert output.trades[0].quantity == 100


def test_max_open_positions_limits_concurrent_entries():
    instruments = [FakeInstrument(f"SYM{i}") for i in range(3)]
    candles = [_candle(i, 100, 101, 99, 100) for i in range(3)]
    candles_by = {str(i.id): candles for i in instruments}
    signals = {str(i.id): BarSignals(entry=[True, False, False], exit=[False, False, False]) for i in instruments}
    sizing = PortfolioSizing(position_size_pct=10.0, max_open_positions=2)

    output = simulate_portfolio(instruments, candles_by, signals, 100000, sizing, NO_RISK, NO_COSTS)

    assert len(output.trades) == 2  # third candidate had no free slot


def test_higher_position_score_wins_scarce_slot():
    # Both fire an entry signal on the same bar, but only one slot is free.
    # AAA has stronger trailing momentum over the lookback window -> it
    # should win the slot (Amibroker's PositionScore mechanic).
    weak, strong = FakeInstrument("WEAK"), FakeInstrument("STRONG")

    def flat_then_entry(rise_pct: float, n: int = 25):
        candles = []
        price = 100.0
        for i in range(n):
            candles.append(_candle(i, price, price + 1, price - 1, price))
            if i == 19:
                price *= 1 + rise_pct / 100  # jump right at the lookback boundary so score differs
        return candles

    weak_candles = flat_then_entry(1.0)
    strong_candles = flat_then_entry(20.0)
    entry = [False] * 20 + [True] + [False] * 4
    no_exit = [False] * 25
    signals = {
        str(weak.id): BarSignals(entry=entry, exit=no_exit),
        str(strong.id): BarSignals(entry=entry, exit=no_exit),
    }
    candles_by = {str(weak.id): weak_candles, str(strong.id): strong_candles}
    sizing = PortfolioSizing(position_size_pct=90.0, max_open_positions=1)

    output = simulate_portfolio([weak, strong], candles_by, signals, 100000, sizing, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].symbol == "STRONG"


def test_still_open_position_at_end_of_range_is_marked_open_not_force_closed():
    a = FakeInstrument("AAA")
    candles = [_candle(i, 100, 101, 99, 100) for i in range(4)]
    signals = {str(a.id): BarSignals(entry=[True, False, False, False], exit=[False, False, False, False])}

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    trade = output.trades[0]
    assert trade.status == "open"
    assert trade.exit_reason == "open"
    assert trade.exit_ts is None
    assert trade.exit_price is None


def test_as_metrics_input_handles_open_trades_without_crashing():
    a = FakeInstrument("AAA")
    candles = [_candle(i, 100, 101, 99, 105) for i in range(4)]
    signals = {str(a.id): BarSignals(entry=[True, False, False, False], exit=[False, False, False, False])}

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)
    metrics = compute_metrics(as_metrics_input(output, 100000), 100000, "1d")

    assert metrics["num_trades"] == 1
    assert metrics["net_profit"] == pytest.approx(output.final_equity - 100000)


def test_stop_loss_still_applies_per_instrument_in_portfolio():
    a = FakeInstrument("AAA")
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # entry at open=100
        _candle(2, 100, 101, 90, 95),  # low=90 breaches a 5% stop (95)
    ]
    signals = {str(a.id): BarSignals(entry=[True, False, False], exit=[False, False, False])}
    risk = RiskRules(stop_loss_pct=5.0)

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, risk, NO_COSTS)

    assert output.trades[0].exit_reason == "stop_loss"
    assert output.trades[0].exit_price == pytest.approx(95.0)


def test_no_signals_produces_no_trades():
    a, b = FakeInstrument("AAA"), FakeInstrument("BBB")
    candles = [_candle(i, 100, 101, 99, 100) for i in range(5)]
    no_signal = BarSignals(entry=[False] * 5, exit=[False] * 5)
    candles_by = {str(a.id): candles, str(b.id): candles}
    signals = {str(a.id): no_signal, str(b.id): no_signal}

    output = simulate_portfolio([a, b], candles_by, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    assert output.trades == []
    assert output.final_equity == 100000


def test_equity_curve_covers_union_of_all_instrument_timestamps():
    a, b = FakeInstrument("AAA"), FakeInstrument("BBB")
    candles_a = [_candle(i, 100, 101, 99, 100) for i in range(5)]
    candles_b = [_candle(i, 100, 101, 99, 100) for i in range(3)]  # shorter history
    no_signal_a = BarSignals(entry=[False] * 5, exit=[False] * 5)
    no_signal_b = BarSignals(entry=[False] * 3, exit=[False] * 3)

    output = simulate_portfolio(
        [a, b], {str(a.id): candles_a, str(b.id): candles_b},
        {str(a.id): no_signal_a, str(b.id): no_signal_b}, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS,
    )

    assert len(output.equity_curve) == 5  # union of timestamps, not intersection
