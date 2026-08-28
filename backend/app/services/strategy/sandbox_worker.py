"""Runs as a standalone subprocess (never imported into the API process) --
`python -m app.services.strategy.sandbox_worker`. Reads a JSON payload from
stdin, executes user-supplied strategy code inside a RestrictedPython
environment, writes a JSON result to stdout.

Two independent layers of isolation, per PRD section 15.1 / Rule 3 (no
direct arbitrary code execution):
  1. RestrictedPython AST restriction + a safe_builtins runtime with no
     `__import__`, `open`, `exec`, `eval`, or dunder-name access.
  2. A separate OS process (spawned by sandbox.py with a hard timeout),
     so even a RestrictedPython escape can't touch the API process's
     memory, DB connections, or broker credentials.
A production deployment should add a third layer (container/network-
namespace isolation) on top of this -- not built here since this dev
environment has no Docker, but nothing above assumes its absence either.

Two payload modes:
  "signal":   one candles list -> one signal (Strategy Builder validation).
  "backtest": one full candles list -> one signal PER BAR, each computed
      from only that bar and everything before it (`candles[: i + 1]`) --
      this is what makes the backtest engine's "no look-ahead" guarantee
      real rather than asserted. Doing this as one subprocess call instead
      of one call per bar is what keeps a multi-year backtest fast.
"""

import json
import operator
import sys

from RestrictedPython import compile_restricted_exec, safe_globals
from RestrictedPython.Eval import default_guarded_getattr, default_guarded_getitem, default_guarded_getiter
from RestrictedPython.Guards import full_write_guard, guarded_iter_unpack_sequence, guarded_unpack_sequence

_INPLACE_OPS = {
    "+=": operator.iadd, "-=": operator.isub, "*=": operator.imul, "/=": operator.itruediv,
    "//=": operator.ifloordiv, "%=": operator.imod, "**=": operator.ipow,
}


def _inplacevar_(op: str, x, y):
    if op not in _INPLACE_OPS:
        raise ValueError(f"Unsupported in-place operator: {op}")
    return _INPLACE_OPS[op](x, y)


def _build_globals() -> dict:
    glb = dict(safe_globals)
    glb["_getattr_"] = default_guarded_getattr
    glb["_getitem_"] = default_guarded_getitem
    glb["_getiter_"] = default_guarded_getiter
    glb["_iter_unpack_sequence_"] = guarded_iter_unpack_sequence
    glb["_unpack_sequence_"] = guarded_unpack_sequence
    glb["_write_"] = full_write_guard
    glb["_inplacevar_"] = _inplacevar_
    return glb


ALLOWED_SIGNALS = {"BUY", "SELL", "HOLD"}


def _load_generate_signal(code: str):
    """Returns (fn, error). fn is None if compilation/loading failed."""
    compiled = compile_restricted_exec(code, filename="<strategy>")
    if compiled.errors:
        return None, "Restricted syntax: " + "; ".join(compiled.errors)

    glb = _build_globals()
    loc: dict = {}
    try:
        exec(compiled.code, glb, loc)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    fn = loc.get("generate_signal")
    if not callable(fn):
        return None, "Strategy code must define generate_signal(candles, params)"
    return fn, None


def run_signal(code: str, candles: list[dict], params: dict) -> dict:
    fn, error = _load_generate_signal(code)
    if error:
        return {"signal": None, "error": error}

    try:
        signal = fn(candles, params)
    except Exception as exc:
        return {"signal": None, "error": f"{type(exc).__name__}: {exc}"}

    if signal not in ALLOWED_SIGNALS:
        return {"signal": None, "error": f"generate_signal must return one of {sorted(ALLOWED_SIGNALS)}, got {signal!r}"}
    return {"signal": signal, "error": None}


def run_backtest_signals(code: str, candles: list[dict], params: dict, warmup: int) -> dict:
    fn, error = _load_generate_signal(code)
    if error:
        return {"signals": None, "error": error}

    signals: list[str] = []
    for i in range(len(candles)):
        if i < warmup:
            signals.append("HOLD")
            continue
        try:
            signal = fn(candles[: i + 1], params)
        except Exception as exc:
            return {"signals": None, "error": f"{type(exc).__name__}: {exc} (at bar {i})"}
        if signal not in ALLOWED_SIGNALS:
            return {"signals": None, "error": f"generate_signal returned {signal!r} at bar {i}, expected {sorted(ALLOWED_SIGNALS)}"}
        signals.append(signal)

    return {"signals": signals, "error": None}


def main() -> None:
    payload = json.loads(sys.stdin.read())
    mode = payload.get("mode", "signal")
    if mode == "backtest":
        result = run_backtest_signals(payload["code"], payload["candles"], payload["params"], payload.get("warmup", 20))
    else:
        result = run_signal(payload["code"], payload["candles"], payload["params"])
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
