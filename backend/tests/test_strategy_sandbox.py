import pytest

from app.services.strategy.sandbox import run_python_strategy
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
