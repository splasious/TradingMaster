import pytest

from app.services.strategy.sandbox import run_python_portfolio_backtest_signals, run_python_strategy
from app.services.strategy.state_machine import StrategyStatus, can_transition

SAMPLE_CANDLES = [{"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 1000.0} for i in range(20)]


async def test_sandbox_runs_legitimate_strategy():
    code = """
def generate_signal(candles, params):
    if candles[-1]["close"] > candles[0]["close"]:
        return "BUY"
    return "HOLD"
"""
    result = await run_python_strategy(code, SAMPLE_CANDLES, {})
    assert result.error is None
    assert result.signal == "BUY"


async def test_sandbox_supports_loops_and_augmented_assignment():
    code = """
def generate_signal(candles, params):
    total = 0
    for c in candles:
        total += c["close"]
    avg = total / len(candles)
    return "BUY" if candles[-1]["close"] > avg else "SELL"
"""
    result = await run_python_strategy(code, SAMPLE_CANDLES, {})
    assert result.error is None
    assert result.signal in ("BUY", "SELL")


async def test_sandbox_uses_params():
    code = """
def generate_signal(candles, params):
    threshold = params.get("threshold", 0)
    return "BUY" if candles[-1]["close"] > threshold else "HOLD"
"""
    result = await run_python_strategy(code, SAMPLE_CANDLES, {"threshold": 1000000})
    assert result.signal == "HOLD"


@pytest.mark.parametrize(
    "code",
    [
        'import os\ndef generate_signal(candles, params):\n    os.system("echo pwned")\n    return "HOLD"',
        'def generate_signal(candles, params):\n    return open("secret.txt").read()',
        'def generate_signal(candles, params):\n    return __import__("os").getcwd()',
        'def generate_signal(candles, params):\n    return eval("1+1")',
        'def generate_signal(candles, params):\n    exec("x=1")\n    return "HOLD"',
    ],
)
async def test_sandbox_blocks_dangerous_operations(code):
    result = await run_python_strategy(code, SAMPLE_CANDLES, {})
    assert result.error is not None
    assert result.signal is None


async def test_sandbox_requires_generate_signal_function():
    result = await run_python_strategy("x = 1", SAMPLE_CANDLES, {})
    assert result.error is not None
    assert "generate_signal" in result.error


async def test_sandbox_rejects_invalid_signal_value():
    code = 'def generate_signal(candles, params):\n    return "MAYBE"'
    result = await run_python_strategy(code, SAMPLE_CANDLES, {})
    assert result.error is not None


async def test_sandbox_enforces_timeout():
    code = """
def generate_signal(candles, params):
    i = 0
    while True:
        i += 1
    return "HOLD"
"""
    result = await run_python_strategy(code, SAMPLE_CANDLES, {}, timeout=1.0)
    assert result.timed_out is True
    assert result.signal is None


async def test_sandbox_reports_syntax_errors_without_crashing():
    result = await run_python_strategy("def generate_signal(:\n  pass", SAMPLE_CANDLES, {})
    assert result.error is not None
    assert result.signal is None


# --- batched portfolio backtest (one subprocess for many instruments) ---


async def test_portfolio_backtest_batch_computes_signals_for_every_instrument():
    code = """
def generate_signal(candles, params):
    return "BUY" if candles[-1]["close"] > 100 else "HOLD"
"""
    instruments = {
        "above": [{"open": 99, "high": 101, "low": 99, "close": c, "volume": 10.0} for c in [95, 96, 105, 106]],
        "below": [{"open": 99, "high": 101, "low": 99, "close": c, "volume": 10.0} for c in [50, 51, 52, 53]],
    }
    result = await run_python_portfolio_backtest_signals(code, instruments, {}, warmup=1)
    assert result.error is None
    assert result.results["above"].signals == ["HOLD", "HOLD", "BUY", "BUY"]
    assert result.results["below"].signals == ["HOLD", "HOLD", "HOLD", "HOLD"]
    assert result.results["above"].error is None
    assert result.results["below"].error is None


async def test_portfolio_backtest_batch_isolates_one_instruments_runtime_error():
    """A strategy bug that only manifests for one instrument's data (e.g.
    dividing by something that's zero only there) must not take down every
    other instrument's already-computed result -- the actual behavior a
    500-instrument batch depends on to be useful at all."""
    code = """
def generate_signal(candles, params):
    return "BUY" if 100 / candles[-1]["close"] > 1 else "HOLD"
"""
    instruments = {
        "fine": [{"open": 1, "high": 1, "low": 1, "close": 50.0, "volume": 10.0}],
        "zero_close": [{"open": 1, "high": 1, "low": 1, "close": 0.0, "volume": 10.0}],
    }
    result = await run_python_portfolio_backtest_signals(code, instruments, {}, warmup=0)
    assert result.error is None
    assert result.results["fine"].error is None
    assert result.results["fine"].signals == ["BUY"]
    assert result.results["zero_close"].error is not None
    assert "ZeroDivisionError" in result.results["zero_close"].error


async def test_portfolio_backtest_batch_whole_batch_error_on_compile_failure():
    result = await run_python_portfolio_backtest_signals(
        "def generate_signal(:\n  pass", {"a": SAMPLE_CANDLES, "b": SAMPLE_CANDLES}, {}, warmup=1,
    )
    assert result.error is not None
    assert result.results is None


async def test_portfolio_backtest_batch_rejects_invalid_signal_value_per_instrument():
    code = 'def generate_signal(candles, params):\n    return "MAYBE"'
    result = await run_python_portfolio_backtest_signals(code, {"a": SAMPLE_CANDLES}, {}, warmup=1)
    assert result.error is None
    assert result.results["a"].signals is None
    assert result.results["a"].error is not None


# --- state machine (PRD section 25) ---


def test_state_machine_forward_progress_allowed():
    assert can_transition(StrategyStatus.DRAFT, StrategyStatus.BACKTESTED)
    assert can_transition(StrategyStatus.APPROVED, StrategyStatus.LIVE)


def test_state_machine_cannot_skip_stages():
    assert not can_transition(StrategyStatus.DRAFT, StrategyStatus.LIVE)
    assert not can_transition(StrategyStatus.DRAFT, StrategyStatus.APPROVED)


def test_state_machine_any_stage_can_return_to_draft():
    for status in StrategyStatus:
        if status == StrategyStatus.DRAFT:
            continue
        assert can_transition(status, StrategyStatus.DRAFT)


def test_state_machine_live_cannot_jump_elsewhere():
    assert not can_transition(StrategyStatus.LIVE, StrategyStatus.APPROVED)
    assert not can_transition(StrategyStatus.LIVE, StrategyStatus.BACKTESTED)


def test_state_machine_backtested_can_go_straight_to_paper_trading():
    # Optimization/out-of-sample testing are optional analysis tools, not
    # mandatory gates -- a backtested strategy can go straight to paper
    # trading without them.
    assert can_transition(StrategyStatus.BACKTESTED, StrategyStatus.PAPER_TRADING)
    assert can_transition(StrategyStatus.OPTIMIZED, StrategyStatus.PAPER_TRADING)


def test_state_machine_still_cannot_skip_paper_trading_or_validation():
    assert not can_transition(StrategyStatus.BACKTESTED, StrategyStatus.VALIDATED)
    assert not can_transition(StrategyStatus.BACKTESTED, StrategyStatus.APPROVED)
    assert not can_transition(StrategyStatus.BACKTESTED, StrategyStatus.LIVE)
    assert not can_transition(StrategyStatus.PAPER_TRADING, StrategyStatus.APPROVED)
