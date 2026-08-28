import asyncio
import json
import sys
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 5.0


@dataclass
class SandboxResult:
    signal: str | None
    error: str | None
    timed_out: bool = False


async def run_python_strategy(
    code: str, candles: list[dict], params: dict, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> SandboxResult:
    """Executes strategy code in a fresh subprocess (see sandbox_worker.py
    for why: it's the actual OS-level isolation boundary, not just the
    RestrictedPython layer inside it)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.services.strategy.sandbox_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    payload = json.dumps({"code": code, "candles": candles, "params": params}).encode()

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return SandboxResult(signal=None, error=f"Strategy execution exceeded {timeout}s timeout", timed_out=True)

    if proc.returncode != 0:
        return SandboxResult(signal=None, error=f"Sandbox process failed: {stderr.decode(errors='replace')[:500]}")

    try:
        result = json.loads(stdout.decode())
    except json.JSONDecodeError:
        return SandboxResult(signal=None, error=f"Sandbox produced invalid output: {stdout.decode(errors='replace')[:500]}")

    return SandboxResult(signal=result.get("signal"), error=result.get("error"))
