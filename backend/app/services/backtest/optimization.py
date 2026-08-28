"""Grid search parameter optimization (PRD section 20). Python-strategy
only -- `generate_signal(candles, params)` already takes a params dict, so
a grid search is just "run the same backtest many times with different
params and rank the results." Visual-mode rule conditions use literal
values, not named parameters, so they're out of scope here; extending the
rule DSL to support named/optimizable thresholds is a real feature, not
attempted in this phase.
"""

import itertools
from dataclasses import dataclass

MAX_COMBINATIONS = 60  # bounds worst-case runtime (each combo re-runs the full backtest)


@dataclass
class ParamRange:
    name: str
    min: float
    max: float
    step: float


class GridTooLargeError(Exception):
    pass


def build_param_grid(ranges: list[ParamRange]) -> list[dict[str, float]]:
    axes = []
    for r in ranges:
        if r.step <= 0:
            raise ValueError(f"Parameter '{r.name}' step must be positive")
        values = []
        v = r.min
        while v <= r.max + 1e-9:
            values.append(round(v, 8))
            v += r.step
        axes.append(values)

    total = 1
    for axis in axes:
        total *= max(1, len(axis))
    if total > MAX_COMBINATIONS:
        raise GridTooLargeError(
            f"Grid has {total} combinations, exceeding the cap of {MAX_COMBINATIONS}. Use fewer parameters or wider steps."
        )

    names = [r.name for r in ranges]
    return [dict(zip(names, combo)) for combo in itertools.product(*axes)]
