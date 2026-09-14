import asyncio
import json
import sys
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 5.0
BACKTEST_TIMEOUT_SECONDS = 60.0
# A portfolio-backtest batch call does BATCH_SIZE instruments' worth of work
# in one subprocess -- scale its timeout with the batch instead of reusing
# the flat single-instrument BACKTEST_TIMEOUT_SECONDS, which would falsely
# time out a legitimately-busy large batch.
#
# 2.0s/instrument was an initial guess tuned against simple test strategies
# (an O(1)-per-bar threshold check) and turned out badly wrong for a real
# indicator-heavy strategy: a ported RSI+Supertrend Python strategy, which
# must replay its full indicator history from scratch on every bar (no
# persisted state between generate_signal calls -- that's what makes
# "no look-ahead" real rather than asserted), measured at ~3s/instrument
# on ~1200-candle real production data and up to ~13s/instrument at the
# MAX_CANDLES=3000 cap -- confirmed live when a production optimization
# job failed every single parameter combination because every 25-instrument
# batch silently exceeded the old 2.0s/instrument timeout (50s allowed vs.
# ~75-325s actually needed) and was killed before producing any signals.
PORTFOLIO_BATCH_TIMEOUT_PER_INSTRUMENT = 15.0
PORTFOLIO_BATCH_MIN_TIMEOUT = 30.0


@dataclass
class SandboxResult:
    signal: str | None
    error: str | None
    timed_out: bool = False


@dataclass
class SandboxBacktestResult:
    signals: list[str] | None
    error: str | None
    timed_out: bool = False


@dataclass
class SandboxPortfolioBacktestResult:
    results: dict[str, SandboxBacktestResult] | None
    error: str | None
    timed_out: bool = False


def _worker_command() -> list[str]:
    """Dev mode: a fresh `python -m sandbox_worker` process. Frozen (packaged
    desktop build): sys.executable is the app's own onefile exe, not a
    python.exe that understands `-m` -- re-invoke that same exe with a flag
    it recognizes instead (see packaging/backend_entry.py)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--sandbox-worker"]
    return [sys.executable, "-m", "app.services.strategy.sandbox_worker"]


async def _run_worker(payload: dict, timeout: float) -> tuple[dict | None, str | None, bool]:
    proc = await asyncio.create_subprocess_exec(
        *_worker_command(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    data = json.dumps(payload).encode()

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(data), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, f"Strategy execution exceeded {timeout}s timeout", True

    if proc.returncode != 0:
        return None, f"Sandbox process failed: {stderr.decode(errors='replace')[:500]}", False

    try:
        return json.loads(stdout.decode()), None, False
    except json.JSONDecodeError:
        return None, f"Sandbox produced invalid output: {stdout.decode(errors='replace')[:500]}", False


async def run_python_strategy(
    code: str, candles: list[dict], params: dict, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> SandboxResult:
    """Executes strategy code in a fresh subprocess (see sandbox_worker.py
    for why: it's the actual OS-level isolation boundary, not just the
    RestrictedPython layer inside it)."""
    result, error, timed_out = await _run_worker({"mode": "signal", "code": code, "candles": candles, "params": params}, timeout)
    if error:
        return SandboxResult(signal=None, error=error, timed_out=timed_out)
    return SandboxResult(signal=result.get("signal"), error=result.get("error"))


async def run_python_backtest_signals(
    code: str, candles: list[dict], params: dict, warmup: int = 20, timeout: float = BACKTEST_TIMEOUT_SECONDS
) -> SandboxBacktestResult:
    """One subprocess call computes a signal for every bar, each restricted
    to that bar and everything before it -- see sandbox_worker.py's
    run_backtest_signals for the actual prefix-slicing that enforces this."""
    result, error, timed_out = await _run_worker(
        {"mode": "backtest", "code": code, "candles": candles, "params": params, "warmup": warmup}, timeout
    )
    if error:
        return SandboxBacktestResult(signals=None, error=error, timed_out=timed_out)
    return SandboxBacktestResult(signals=result.get("signals"), error=result.get("error"))


async def run_python_portfolio_backtest_signals(
    code: str, instruments: dict[str, list[dict]], params: dict, warmup: int = 20,
) -> SandboxPortfolioBacktestResult:
    """Batched sibling of run_python_backtest_signals -- one subprocess call
    computes signals for every instrument in `instruments`
    ({instrument_id: candles}), compiling generate_signal once and reusing
    it across all of them (sandbox_worker.py::run_portfolio_backtest_signals).
    This is the actual fix for a large portfolio backtest spawning one
    process (and recompiling identical code) per instrument -- see
    services/backtest/portfolio_runner.py's call site for the batching/
    concurrency this is designed to be called under."""
    timeout = max(PORTFOLIO_BATCH_MIN_TIMEOUT, PORTFOLIO_BATCH_TIMEOUT_PER_INSTRUMENT * len(instruments))
    payload_instruments = {inst_id: {"candles": candles} for inst_id, candles in instruments.items()}
    result, error, timed_out = await _run_worker(
        {"mode": "portfolio_backtest", "code": code, "instruments": payload_instruments, "params": params, "warmup": warmup},
        timeout,
    )
    if error:
        return SandboxPortfolioBacktestResult(results=None, error=error, timed_out=timed_out)

    raw_results = result.get("results")
    if raw_results is None:
        # Whole-batch failure (e.g. the code failed to compile at all) --
        # preserve None rather than collapsing it into an empty dict, the
        # same "no results at all" vs. "zero instruments" distinction
        # SandboxBacktestResult.signals already makes for one instrument.
        return SandboxPortfolioBacktestResult(results=None, error=result.get("error"))

    parsed = {
        inst_id: SandboxBacktestResult(signals=r.get("signals"), error=r.get("error"))
        for inst_id, r in raw_results.items()
    }
    return SandboxPortfolioBacktestResult(results=parsed, error=result.get("error"))
