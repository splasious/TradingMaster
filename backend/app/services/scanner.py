"""Market scanner (PRD section 33).

Filters are a structured, safe DSL -- {field, operator, value} triples
evaluated in Python against known fields only. This deliberately never
evals user-supplied expressions (PRD Rule 3: no arbitrary code execution).
"""

import operator as op

from app.services.indicators.base import candles_to_frame
from app.services.indicators.registry import get_indicator

RAW_FIELDS = ("open", "high", "low", "close", "volume")

_OPERATORS = {
    ">": op.gt,
    "<": op.lt,
    ">=": op.ge,
    "<=": op.le,
    "==": op.eq,
}


def evaluate_field(candles, field: str) -> float | None:
    """Returns the most recent value of `field` for this candle series, or
    None if there isn't enough history to compute it."""
    if not candles:
        return None

    if field in RAW_FIELDS:
        return float(getattr(candles[-1], field))

    if "." not in field:
        raise ValueError(f"Indicator fields must be 'indicator_code.output_field' (got '{field}')")
    code, output_field = field.split(".", 1)

    spec = get_indicator(code)
    if output_field not in spec.output_fields:
        raise ValueError(f"Indicator '{code}' has no output '{output_field}' (has: {spec.output_fields})")

    df = candles_to_frame(candles)
    result = spec.compute(df)
    value = result[output_field].iloc[-1]
    return None if value != value else float(value)  # NaN check without importing pandas/numpy here


def evaluate_condition(candles, condition) -> tuple[bool, float | None]:
    value = evaluate_field(candles, condition.field)
    if value is None:
        return False, None
    return _OPERATORS[condition.operator](value, condition.value), value
