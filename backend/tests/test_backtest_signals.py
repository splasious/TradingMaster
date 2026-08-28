import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.market_data import OhlcvCandle
from app.services.backtest.signals import (
    WARMUP_BARS,
    SignalComputationError,
    compute_python_signals,
    compute_visual_signals,
)


def _rising_candles(n=40):
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [
        OhlcvCandle(
            instrument_id=uuid.uuid4(), timeframe="1d", ts=base + timedelta(days=i), open=100 + i, high=101 + i,
            low=99 + i, close=100 + i, volume=1000.0, source="test",
        )
        for i in range(n)
    ]


def test_visual_signals_are_false_during_warmup():
    candles = _rising_candles(40)
    entry_rules = {"all": [{"field": "close", "operator": ">", "value": 0}]}
    exit_rules = {"all": [{"field": "close", "operator": "<", "value": 0}]}
    signals = compute_visual_signals(candles, entry_rules, exit_rules)
    assert all(v is False for v in signals.entry[:WARMUP_BARS])
    assert all(v is False for v in signals.exit[:WARMUP_BARS])
    assert len(signals.entry) == len(candles)


def test_visual_signals_react_after_warmup():
    candles = _rising_candles(40)
    entry_rules = {"all": [{"field": "rsi.rsi", "operator": ">", "value": 50}]}
    exit_rules = {"all": [{"field": "rsi.rsi", "operator": "<", "value": 10}]}
    signals = compute_visual_signals(candles, entry_rules, exit_rules)
    assert any(signals.entry[WARMUP_BARS:])  # strictly rising closes -> high RSI eventually


def test_visual_signals_raises_on_invalid_rule():
    candles = _rising_candles(40)
    with pytest.raises(SignalComputationError):
        compute_visual_signals(candles, {"bogus": "rule"}, {"all": []})


async def test_python_signals_prefix_only_no_lookahead():
    candles = _rising_candles(40)
    # This strategy would only see a future drop if given the full series
    # up front; since each call only gets candles[:i+1], it can never "see"
    # a price drop that hasn't happened yet in the simulation.
    code = """
def generate_signal(candles, params):
    if len(candles) < 2:
        return "HOLD"
    return "BUY" if candles[-1]["close"] > candles[-2]["close"] else "SELL"
"""
    signals = await compute_python_signals(candles, code, {})
    assert len(signals.entry) == len(candles)
    # Strictly rising series -> every post-warmup bar is higher than the
    # previous one -> should be BUY (entry=True) everywhere after warmup.
    assert all(signals.entry[WARMUP_BARS:])
    assert not any(signals.exit[WARMUP_BARS:])


async def test_python_signals_reports_sandbox_errors():
    candles = _rising_candles(40)
    with pytest.raises(SignalComputationError):
        await compute_python_signals(candles, "def generate_signal(c, p):\n    return 1/0", {})
