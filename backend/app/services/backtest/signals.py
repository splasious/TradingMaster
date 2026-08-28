"""Turns a strategy definition into one BUY/SELL/HOLD-shaped pair of arrays
(entry[i], exit[i]) aligned to the candle list, with the "no look-ahead"
guarantee enforced by construction rather than merely asserted: every
value at index i comes from evaluating the strategy against
`candles[: i + 1]` only, whether that's the visual rule-tree evaluator or
the sandboxed Python path (see sandbox_worker.py's run_backtest_signals).

Recomputes indicators from scratch on the growing prefix for every bar in
visual mode -- O(n^2) rolling-window calls rather than an incrementally
updated O(n). Correct and simple; a real performance concern only past a
few thousand bars, which is why the backtest API caps candle count.
"""

from dataclasses import dataclass

from app.models.market_data import OhlcvCandle
from app.services.strategy.rules import evaluate_rule_node
from app.services.strategy.sandbox import run_python_backtest_signals

WARMUP_BARS = 20


@dataclass
class BarSignals:
    entry: list[bool]
    exit: list[bool]


class SignalComputationError(Exception):
    pass


def compute_visual_signals(candles: list[OhlcvCandle], entry_rules: dict, exit_rules: dict) -> BarSignals:
    entry: list[bool] = []
    exit_: list[bool] = []
    for i in range(len(candles)):
        if i < WARMUP_BARS:
            entry.append(False)
            exit_.append(False)
            continue
        prefix = candles[: i + 1]
        try:
            entry.append(evaluate_rule_node(prefix, entry_rules))
            exit_.append(evaluate_rule_node(prefix, exit_rules))
        except ValueError as exc:
            raise SignalComputationError(str(exc)) from exc
    return BarSignals(entry=entry, exit=exit_)


async def compute_python_signals(candles: list[OhlcvCandle], python_code: str, params: dict) -> BarSignals:
    bars = [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in candles]
    result = await run_python_backtest_signals(python_code, bars, params, warmup=WARMUP_BARS)
    if result.error:
        raise SignalComputationError(result.error)
    signals = result.signals or []
    return BarSignals(entry=[s == "BUY" for s in signals], exit=[s == "SELL" for s in signals])
