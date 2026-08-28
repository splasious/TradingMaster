import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.market_data import OhlcvCandle
from app.services.strategy.rules import evaluate_rule_node


def _candle(close: float) -> OhlcvCandle:
    return OhlcvCandle(
        instrument_id=uuid.uuid4(), timeframe="1d", ts=datetime.now(timezone.utc), open=close, high=close + 1,
        low=close - 1, close=close, volume=1000.0, source="test",
    )


def _rising_candles(n=20):
    base = datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [
        OhlcvCandle(
            instrument_id=uuid.uuid4(), timeframe="1d", ts=base + timedelta(days=i), open=100 + i, high=101 + i,
            low=99 + i, close=100 + i, volume=1000.0, source="test",
        )
        for i in range(n)
    ]


def test_leaf_condition():
    candles = [_candle(100)]
    assert evaluate_rule_node(candles, {"field": "close", "operator": ">", "value": 50}) is True
    assert evaluate_rule_node(candles, {"field": "close", "operator": ">", "value": 500}) is False


def test_all_requires_every_child():
    candles = _rising_candles(20)
    node = {"all": [{"field": "close", "operator": ">", "value": 0}, {"field": "rsi.rsi", "operator": ">", "value": 90}]}
    assert evaluate_rule_node(candles, node) is True

    node_fail = {"all": [{"field": "close", "operator": ">", "value": 0}, {"field": "rsi.rsi", "operator": "<", "value": 10}]}
    assert evaluate_rule_node(candles, node_fail) is False


def test_any_requires_one_child():
    candles = _rising_candles(20)
    node = {"any": [{"field": "close", "operator": "<", "value": 0}, {"field": "rsi.rsi", "operator": ">", "value": 90}]}
    assert evaluate_rule_node(candles, node) is True


def test_nested_all_any():
    candles = _rising_candles(20)
    node = {
        "all": [
            {"field": "close", "operator": ">", "value": 0},
            {"any": [{"field": "rsi.rsi", "operator": ">", "value": 90}, {"field": "close", "operator": "<", "value": 0}]},
        ]
    }
    assert evaluate_rule_node(candles, node) is True


def test_invalid_node_raises():
    with pytest.raises(ValueError):
        evaluate_rule_node([_candle(100)], {"bogus": "node"})


def test_excessively_deep_nesting_raises():
    node = {"field": "close", "operator": ">", "value": 0}
    for _ in range(10):
        node = {"all": [node]}
    with pytest.raises(ValueError):
        evaluate_rule_node([_candle(100)], node)
