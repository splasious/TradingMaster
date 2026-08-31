import asyncio
import json
import sys
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 5.0
BACKTEST_TIMEOUT_SECONDS = 60.0


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
