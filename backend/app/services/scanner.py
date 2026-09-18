"""Market scanner (PRD section 33).

Filters are a structured, safe DSL -- {field, operator, value} triples
evaluated in Python against known fields only. This deliberately never
evals user-supplied expressions (PRD Rule 3: no arbitrary code execution).

run_python_strategy_scan() below is the one deliberate exception: it runs
a saved Python STRATEGY's own generate_signal(candles, params) against
many instruments to answer "what does this strategy say right now" --
safe not because it avoids code execution (it doesn't) but because it
reuses the same sandboxed, batched execution path already trusted for
backtesting/paper/live trading (RestrictedPython + a throwaway OS
subprocess per batch, see services/strategy/sandbox_worker.py), not a
new code-execution surface.
"""

import operator as op

from app.services.indicators.base import candles_to_frame
from app.services.indicators.registry import get_indicator

RAW_FIELDS = ("open", "high", "low", "close", "volume")
# Same caps as the backtest/optimization runners -- MIN_CANDLES is the
# floor below which a strategy's indicators (e.g. an RSI/ATR warmup)
# can't have produced a meaningful signal yet; MAX_CANDLES bounds a
# single scan call's worst-case per-instrument cost.
SCAN_MAX_CANDLES = 3000
SCAN_MIN_CANDLES = 30

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


async def run_python_strategy_scan(
    db, version, instruments: list, timeframe_override: str | None = None
) -> tuple[dict[str, str], list[str]]:
    """Runs a Python strategy's current signal against every instrument in
    `instruments`, batched (see module docstring). Returns
    (signal_by_instrument_id, skipped_symbols) -- an instrument is
    skipped for too little history to warm up the strategy's indicators,
    or for its own per-instrument error (a strategy bug that only bites
    on that instrument's data), same "isolate, don't fail the whole scan"
    behavior the backtest/optimization batch helpers already have.
    `timeframe_override`, when given, is scanned instead of the strategy
    version's own saved timeframe -- lets a scan check "what would this
    strategy say on a different timeframe" without editing the strategy."""
    # Local import: services.strategy.rules imports evaluate_condition
    # from this module, so a module-level import here of anything that
    # (transitively) imports rules.py would be a circular import.
    from app.services.backtest.candle_source import load_candles
    from app.services.backtest.signals import compute_python_signal_for_portfolio

    candles_by_instrument: dict[str, list] = {}
    inst_by_id = {}
    skipped: list[str] = []

    for instrument in instruments:
        candles = (await load_candles(db, instrument.id, timeframe_override or version.timeframe))[-SCAN_MAX_CANDLES:]
        if len(candles) < SCAN_MIN_CANDLES:
            skipped.append(instrument.symbol)
            continue
        inst_id = str(instrument.id)
        candles_by_instrument[inst_id] = candles
        inst_by_id[inst_id] = instrument

    if not candles_by_instrument:
        return {}, skipped

    signal_by_instrument, errors_by_instrument = await compute_python_signal_for_portfolio(
        candles_by_instrument, version.python_code, version.parameters
    )
    for inst_id in errors_by_instrument:
        skipped.append(inst_by_id[inst_id].symbol)
    return signal_by_instrument, skipped
