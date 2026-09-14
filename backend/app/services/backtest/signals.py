"""Turns a strategy definition into BUY/SELL/SHORT/COVER/HOLD-shaped arrays
aligned to the candle list, with the "no look-ahead" guarantee enforced by
construction rather than merely asserted: every value at index i comes
from evaluating the strategy against `candles[: i + 1]` only, whether
that's the visual rule-tree evaluator or the sandboxed Python path (see
sandbox_worker.py's run_backtest_signals).

SHORT/COVER (opening/closing a short position) is Python-strategy only --
visual mode's rule DSL has no short-side primitive, so compute_visual_signals
always reports short_entry/short_exit as all-False (a visual strategy is
long-only, exactly as before this pair was added).

Recomputes indicators from scratch on the growing prefix for every bar in
visual mode -- O(n^2) rolling-window calls rather than an incrementally
updated O(n). Correct and simple; a real performance concern only past a
few thousand bars, which is why the backtest API caps candle count.
"""

import asyncio
from dataclasses import dataclass

from app.models.market_data import OhlcvCandle
from app.services.strategy.rules import evaluate_rule_node
from app.services.strategy.sandbox import (
    run_python_backtest_signals,
    run_python_portfolio_backtest_signals,
    run_python_portfolio_signals,
)

WARMUP_BARS = 20
# Shared by every caller that needs signals for a whole basket of
# instruments at once (portfolio_runner.py: one call per job;
# portfolio_optimization_runner.py: one call per parameter combination --
# where this matters even more, since a MAX_COMBINATIONS=60 grid search
# without this batching would be 60x the single-backtest version of the
# "500 subprocess spawns" bug this was built to fix).
PORTFOLIO_BATCH_SIZE = 25
MAX_CONCURRENT_BATCHES = 4


@dataclass
class BarSignals:
    entry: list[bool]
    exit: list[bool]
    # None (the default) means "no short-side signals at all" -- visual
    # strategies and any existing caller constructing BarSignals with just
    # entry/exit keep working unchanged; the engines treat a None list the
    # same as a same-length all-False list at every bar.
    short_entry: list[bool] | None = None
    short_exit: list[bool] | None = None


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


def _candles_to_bars(candles: list[OhlcvCandle]) -> list[dict]:
    return [
        {"ts": c.ts.isoformat(), "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
        for c in candles
    ]


def _signal_strings_to_bar_signals(signals: list[str]) -> BarSignals:
    return BarSignals(
        entry=[s == "BUY" for s in signals], exit=[s == "SELL" for s in signals],
        short_entry=[s == "SHORT" for s in signals], short_exit=[s == "COVER" for s in signals],
    )


async def compute_python_signals(candles: list[OhlcvCandle], python_code: str, params: dict) -> BarSignals:
    result = await run_python_backtest_signals(python_code, _candles_to_bars(candles), params, warmup=WARMUP_BARS)
    if result.error:
        raise SignalComputationError(result.error)
    return _signal_strings_to_bar_signals(result.signals or [])


async def compute_python_signals_batch(
    candles_by_instrument: dict[str, list[OhlcvCandle]], python_code: str, params: dict,
) -> tuple[dict[str, BarSignals], dict[str, str]]:
    """Batched sibling of compute_python_signals -- one subprocess call for
    the whole batch (services/strategy/sandbox.py::run_python_portfolio_backtest_signals)
    instead of one per instrument, the actual fix for a large portfolio
    backtest being extremely slow (500 process spawns + 500 redundant
    RestrictedPython compilations of identical code, sequentially).

    Returns (signals_by_instrument, errors_by_instrument) rather than
    raising -- a single instrument's own error (or a whole-batch error,
    e.g. the code failed to compile at all) must not take down every
    other instrument's already-computed result; the caller decides what
    "skip this instrument" means for a portfolio backtest, same as
    compute_python_signals' per-instrument SignalComputationError already
    means for the non-batched path."""
    bars_by_instrument = {inst_id: _candles_to_bars(candles) for inst_id, candles in candles_by_instrument.items()}
    result = await run_python_portfolio_backtest_signals(python_code, bars_by_instrument, params, warmup=WARMUP_BARS)

    if result.error:
        # Whole-batch failure (e.g. a compile error) -- every instrument in
        # this batch gets the same error, none get a signal.
        return {}, {inst_id: result.error for inst_id in candles_by_instrument}

    signals_by_instrument: dict[str, BarSignals] = {}
    errors_by_instrument: dict[str, str] = {}
    for inst_id, r in (result.results or {}).items():
        if r.error:
            errors_by_instrument[inst_id] = r.error
        else:
            signals_by_instrument[inst_id] = _signal_strings_to_bar_signals(r.signals or [])
    return signals_by_instrument, errors_by_instrument


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def compute_python_signals_for_portfolio(
    candles_by_instrument: dict[str, list[OhlcvCandle]], python_code: str, params: dict,
) -> tuple[dict[str, BarSignals], dict[str, str]]:
    """Chunks candles_by_instrument into PORTFOLIO_BATCH_SIZE-sized batches
    and runs them through compute_python_signals_batch with bounded
    concurrency (MAX_CONCURRENT_BATCHES) -- the actual fix for a large
    portfolio's Python-strategy signal computation being extremely slow,
    factored out so every caller that needs "signals for a whole basket"
    (a single portfolio backtest, or one combo of a parameter grid search)
    gets it, not just the first one that needed it."""
    batches = _chunk(list(candles_by_instrument.items()), PORTFOLIO_BATCH_SIZE)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)

    async def _run_batch(batch: list[tuple[str, list]]) -> tuple[dict, dict]:
        async with semaphore:
            return await compute_python_signals_batch(dict(batch), python_code, params)

    batch_results = await asyncio.gather(*(_run_batch(b) for b in batches))

    signals_by_instrument: dict[str, BarSignals] = {}
    errors_by_instrument: dict[str, str] = {}
    for batch_signals, batch_errors in batch_results:
        signals_by_instrument.update(batch_signals)
        errors_by_instrument.update(batch_errors)
    return signals_by_instrument, errors_by_instrument


async def compute_python_signal_batch(
    candles_by_instrument: dict[str, list[OhlcvCandle]], python_code: str, params: dict,
) -> tuple[dict[str, str], dict[str, str]]:
    """Market-scanner sibling of compute_python_signals_batch -- one
    subprocess call, one CURRENT signal per instrument (not a full
    per-bar backtest series). Returns (signal_by_instrument,
    errors_by_instrument), same never-raises contract as the backtest
    batch helpers."""
    bars_by_instrument = {inst_id: _candles_to_bars(candles) for inst_id, candles in candles_by_instrument.items()}
    result = await run_python_portfolio_signals(python_code, bars_by_instrument, params)

    if result.error:
        return {}, {inst_id: result.error for inst_id in candles_by_instrument}

    signal_by_instrument: dict[str, str] = {}
    errors_by_instrument: dict[str, str] = {}
    for inst_id, r in (result.results or {}).items():
        if r.error:
            errors_by_instrument[inst_id] = r.error
        else:
            signal_by_instrument[inst_id] = r.signal
    return signal_by_instrument, errors_by_instrument


async def compute_python_signal_for_portfolio(
    candles_by_instrument: dict[str, list[OhlcvCandle]], python_code: str, params: dict,
) -> tuple[dict[str, str], dict[str, str]]:
    """Chunked + concurrent sibling of compute_python_signals_for_portfolio,
    for the market scanner: one latest signal per instrument across
    potentially thousands of instruments, batched the same way to avoid
    one subprocess spawn per instrument (the same class of bug already
    fixed for portfolio backtests and optimization)."""
    batches = _chunk(list(candles_by_instrument.items()), PORTFOLIO_BATCH_SIZE)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)

    async def _run_batch(batch: list[tuple[str, list]]) -> tuple[dict, dict]:
        async with semaphore:
            return await compute_python_signal_batch(dict(batch), python_code, params)

    batch_results = await asyncio.gather(*(_run_batch(b) for b in batches))

    signal_by_instrument: dict[str, str] = {}
    errors_by_instrument: dict[str, str] = {}
    for batch_signals, batch_errors in batch_results:
        signal_by_instrument.update(batch_signals)
        errors_by_instrument.update(batch_errors)
    return signal_by_instrument, errors_by_instrument
