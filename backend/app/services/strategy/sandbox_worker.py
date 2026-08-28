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


def run(code: str, candles: list[dict], params: dict) -> dict:
    compiled = compile_restricted_exec(code, filename="<strategy>")
    if compiled.errors:
        return {"signal": None, "error": "Restricted syntax: " + "; ".join(compiled.errors)}

    glb = _build_globals()
    loc: dict = {}
    try:
        exec(compiled.code, glb, loc)
    except Exception as exc:
        return {"signal": None, "error": f"{type(exc).__name__}: {exc}"}

    generate_signal = loc.get("generate_signal")
    if not callable(generate_signal):
        return {"signal": None, "error": "Strategy code must define generate_signal(candles, params)"}

    try:
        signal = generate_signal(candles, params)
    except Exception as exc:
        return {"signal": None, "error": f"{type(exc).__name__}: {exc}"}

    if signal not in ALLOWED_SIGNALS:
        return {"signal": None, "error": f"generate_signal must return one of {sorted(ALLOWED_SIGNALS)}, got {signal!r}"}

    return {"signal": signal, "error": None}


def main() -> None:
    payload = json.loads(sys.stdin.read())
    result = run(payload["code"], payload["candles"], payload["params"])
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
