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


# ---------------------------------------------------------------- shorts --


def _no_signal(n: int) -> list[bool]:
    return [False] * n


def test_short_and_long_can_be_concurrently_open_from_one_pool():
    up, down = FakeInstrument("UPUSD"), FakeInstrument("DOWNUSD")
    n = 5
    up_candles = [_candle(i, 100, 101, 99, 100) for i in range(n)]
    down_candles = [_candle(i, 200, 202, 198, 200) for i in range(n)]
    long_signal = BarSignals(entry=[True] + [False] * (n - 1), exit=_no_signal(n))
    short_signal = BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True] + [False] * (n - 1), short_exit=_no_signal(n))
    candles_by = {str(up.id): up_candles, str(down.id): down_candles}
    signals = {str(up.id): long_signal, str(down.id): short_signal}

    output = simulate_portfolio([up, down], candles_by, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    sides = {t.symbol: t.side for t in output.trades}
    assert sides == {"UPUSD": "long", "DOWNUSD": "short"}


def test_short_cover_fills_at_next_bar_open_and_profits_on_price_fall():
    a = FakeInstrument("AAA")
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # short entry fills here (open=100)
        _candle(2, 90, 91, 89, 90),  # cover signal fires here (based on this bar's close)
        _candle(3, 80, 81, 79, 80),  # cover must fill HERE at open=80
    ]
    n = len(candles)
    signals = {
        str(a.id): BarSignals(
            entry=_no_signal(n), exit=_no_signal(n),
            short_entry=[True, False, False, False], short_exit=[False, False, True, False],
        )
    }
    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    trade = output.trades[0]
    assert trade.side == "short"
    assert trade.entry_price == 100
    assert trade.exit_price == 80
    assert trade.status == "closed"
    assert trade.pnl > 0  # covered lower than entry -> profit on a short


def test_stronger_downward_mover_wins_scarce_slot_over_weaker_upward_mover():
    """A long candidate with weak upward momentum and a short candidate
    with strong downward momentum compete for one slot -- the short should
    win, since its conviction (how far price moved in ITS OWN favorable
    direction) is stronger, not because "long beats short" or vice versa."""
    weak_long, strong_short = FakeInstrument("WEAKLONG"), FakeInstrument("STRONGSHORT")

    def make_candles(pct_move: float, n: int = 25) -> list[OhlcvCandle]:
        candles = []
        price = 100.0
        for i in range(n):
            candles.append(_candle(i, price, price + 1, price - 1, price))
            if i == 19:
                price *= 1 + pct_move / 100
        return candles

    weak_long_candles = make_candles(1.0)  # +1% at the lookback boundary -- weak
    strong_short_candles = make_candles(-20.0)  # -20% at the lookback boundary -- strong down move

    entry = [False] * 20 + [True] + [False] * 4
    no_exit = [False] * 25
    signals = {
        str(weak_long.id): BarSignals(entry=entry, exit=no_exit),
        str(strong_short.id): BarSignals(entry=no_exit, exit=no_exit, short_entry=entry, short_exit=no_exit),
    }
    candles_by = {str(weak_long.id): weak_long_candles, str(strong_short.id): strong_short_candles}
    sizing = PortfolioSizing(position_size_pct=90.0, max_open_positions=1)

    output = simulate_portfolio([weak_long, strong_short], candles_by, signals, 100000, sizing, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].symbol == "STRONGSHORT"
    assert output.trades[0].side == "short"


def test_short_stop_loss_and_take_profit_are_mirrored():
    a = FakeInstrument("AAA")
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # short entry at open=100
        _candle(2, 100, 105, 99, 103),  # high=105 breaches a 5% stop (105)
    ]
    n = len(candles)
    signals = {str(a.id): BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True, False, False], short_exit=_no_signal(n))}
    risk = RiskRules(stop_loss_pct=5.0)

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, risk, NO_COSTS)

    assert output.trades[0].exit_reason == "stop_loss"
    assert output.trades[0].exit_price == pytest.approx(105.0)
    assert output.trades[0].pnl < 0


def test_still_open_short_at_end_of_range_is_marked_open_not_force_closed():
    a = FakeInstrument("AAA")
    candles = [_candle(i, 100, 101, 99, 100) for i in range(4)]
    n = len(candles)
    signals = {str(a.id): BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True, False, False, False], short_exit=_no_signal(n))}

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    trade = output.trades[0]
    assert trade.side == "short"
    assert trade.status == "open"
    assert trade.exit_reason == "open"
    assert trade.exit_ts is None
    assert trade.exit_price is None


def test_short_signal_while_long_closes_the_long_via_automatic_reversal():
    a = FakeInstrument("AAA")
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # long entry fills here
        _candle(2, 100, 101, 99, 90),  # SHORT signal fires here -- no explicit exit ever fired
        _candle(3, 85, 86, 84, 85),  # long must close HERE at open=85
    ]
    n = len(candles)
    signals = {str(a.id): BarSignals(entry=[True, False, False, False], exit=_no_signal(n), short_entry=[False, False, True, False], short_exit=_no_signal(n))}

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "long"
    assert output.trades[0].exit_price == 85


def test_buy_signal_while_short_covers_via_automatic_reversal():
    a = FakeInstrument("AAA")
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),  # short entry fills here
        _candle(2, 100, 101, 99, 110),  # BUY signal fires here -- no explicit cover ever fired
        _candle(3, 115, 116, 114, 115),  # short must cover HERE at open=115
    ]
    n = len(candles)
    signals = {str(a.id): BarSignals(entry=[False, False, True, False], exit=_no_signal(n), short_entry=[True, False, False, False], short_exit=_no_signal(n))}

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)

    assert len(output.trades) == 1
    assert output.trades[0].side == "short"
    assert output.trades[0].exit_price == 115


def test_breadth_exit_force_closes_longs_when_basket_turns_bearish():
    """All three go long while the whole basket is still rising (breadth
    bullish -- entries are allowed). Then two of the three reverse into a
    real decline -- once the basket-wide trailing-momentum majority turns
    negative, every open long is force-closed, including the one
    instrument still individually trending up, since this is a basket-
    wide switch, not a per-instrument one. None of the three ever gets an
    explicit exit signal of its own."""
    up, down_a, down_b = FakeInstrument("UP"), FakeInstrument("DOWNA"), FakeInstrument("DOWNB")
    n = 70
    flip_bar = 25

    def make_candles(flip_to_down: bool) -> list[OhlcvCandle]:
        candles = []
        price = 100.0
        for i in range(n):
            candles.append(_candle(i, price, price + 1, price - 1, price))
            pct = -1.0 if (flip_to_down and i >= flip_bar) else 1.0
            price *= 1 + pct / 100
        return candles

    up_candles = make_candles(flip_to_down=False)
    down_a_candles = make_candles(flip_to_down=True)
    down_b_candles = make_candles(flip_to_down=True)

    # All three go long on bar 20, while the basket is still uniformly
    # rising -- none ever gets an explicit exit signal.
    entry = [False] * 20 + [True] + [False] * (n - 21)
    no_exit = [False] * n
    candles_by = {str(up.id): up_candles, str(down_a.id): down_a_candles, str(down_b.id): down_b_candles}
    signals = {
        str(up.id): BarSignals(entry=entry, exit=no_exit),
        str(down_a.id): BarSignals(entry=entry, exit=no_exit),
        str(down_b.id): BarSignals(entry=entry, exit=no_exit),
    }
    sizing = PortfolioSizing(position_size_pct=20.0, max_open_positions=10)

    without_breadth_exit = simulate_portfolio([up, down_a, down_b], candles_by, signals, 100000, sizing, NO_RISK, NO_COSTS)
    with_breadth_exit = simulate_portfolio([up, down_a, down_b], candles_by, signals, 100000, sizing, NO_RISK, NO_COSTS, breadth_exit_threshold=1.0)

    # Without the switch, all three entered (breadth was bullish at entry
    # time) and ride to end-of-data (still open) -- no exit signal ever fired.
    assert len(without_breadth_exit.trades) == 3
    assert all(t.status == "open" for t in without_breadth_exit.trades)

    # With it, all three still entered the same way, but once down_a/down_b's
    # decline makes the basket-wide majority bearish, every long -- including
    # the still-rising UP instrument -- gets force-closed.
    assert len(with_breadth_exit.trades) == 3
    assert all(t.exit_reason == "breadth_exit" for t in with_breadth_exit.trades)
    assert all(t.status == "closed" for t in with_breadth_exit.trades)


def test_breadth_exit_blocks_new_long_entries_while_bearish():
    """down_a/down_b decline from the start (no entry signal of their own
    -- present purely to establish a decliner majority once the trailing-
    momentum lookback is satisfied). would_enter tries to go long only
    after that point -- its entry must never fill at all."""
    down_a, down_b, would_enter = FakeInstrument("DOWNA"), FakeInstrument("DOWNB"), FakeInstrument("WOULDENTER")
    n = 30

    def make_declining_candles() -> list[OhlcvCandle]:
        candles = []
        price = 100.0
        for i in range(n):
            candles.append(_candle(i, price, price + 1, price - 1, price))
            price *= 0.99
        return candles

    down_a_candles = make_declining_candles()
    down_b_candles = make_declining_candles()
    would_enter_candles = make_declining_candles()

    no_signal = [False] * n
    # would_enter's own setup fires on bar 22 -- well after bar 20, the
    # first bar with enough history for breadth to have gone bearish.
    late_entry = [False] * 22 + [True] + [False] * (n - 23)
    candles_by = {str(down_a.id): down_a_candles, str(down_b.id): down_b_candles, str(would_enter.id): would_enter_candles}
    signals = {
        str(down_a.id): BarSignals(entry=no_signal, exit=no_signal),
        str(down_b.id): BarSignals(entry=no_signal, exit=no_signal),
        str(would_enter.id): BarSignals(entry=late_entry, exit=no_signal),
    }
    sizing = PortfolioSizing(position_size_pct=20.0, max_open_positions=10)

    output = simulate_portfolio(
        [down_a, down_b, would_enter], candles_by, signals, 100000, sizing, NO_RISK, NO_COSTS, breadth_exit_threshold=1.0,
    )

    assert output.trades == []


def test_breadth_exit_disabled_by_default_matches_prior_behavior():
    a = FakeInstrument("AAA")
    candles = [_candle(i, 100, 101, 99, 100) for i in range(30)]
    signals = {str(a.id): BarSignals(entry=[False] * 25 + [True] + [False] * 4, exit=[False] * 30)}

    default_output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)
    explicit_none_output = simulate_portfolio(
        [a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS, breadth_exit_threshold=None,
    )

    assert len(default_output.trades) == len(explicit_none_output.trades) == 1
    assert default_output.final_equity == explicit_none_output.final_equity


def test_as_metrics_input_handles_open_short_without_crashing():
    a = FakeInstrument("AAA")
    candles = [_candle(i, 100, 101, 99, 95) for i in range(4)]  # price fell -> unrealized profit on the short
    n = len(candles)
    signals = {str(a.id): BarSignals(entry=_no_signal(n), exit=_no_signal(n), short_entry=[True, False, False, False], short_exit=_no_signal(n))}

    output = simulate_portfolio([a], {str(a.id): candles}, signals, 100000, DEFAULT_SIZING, NO_RISK, NO_COSTS)
    metrics = compute_metrics(as_metrics_input(output, 100000), 100000, "1d")

    assert metrics["num_trades"] == 1
    assert metrics["net_profit"] == pytest.approx(output.final_equity - 100000)
