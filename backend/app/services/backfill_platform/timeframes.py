"""Real, per-source native timeframe support -- deliberately not a single
shared list, since the three sources genuinely support different sets
(confirmed live/from each source's own code, not guessed):
  - Yahoo (nse-yahoo-data): 1m,5m,15m,30m,60m,1d,1wk,1mo (its own
    VALID_INTERVALS).
  - Delta: 1m,5m,15m,30m,60m,4h,1d,1wk (its documented candle
    resolutions; no native monthly).
  - Zerodha Kite: 1m,5m,15m,30m,60m,1d (its documented historical
    intervals; no native weekly/monthly).

"45m" isn't offered anywhere -- none of the three sources has a real
45-minute native resolution, and fabricating one via resampling from a
non-divisor base (e.g. 15m -> 45m needs 3 bars to align, which drifts
across session boundaries) isn't something this exposes as if it were a
clean native interval.

Weekly/monthly are additionally available as *derived* views (resampled
from stored daily bars, reusing app.services.market_data.resample) for
sources that don't natively offer them -- flagged accordingly.
"""

from dataclasses import dataclass

_NATIVE: dict[str, list[str]] = {
    "yahoo": ["1m", "5m", "15m", "30m", "60m", "1d", "1wk", "1mo"],
    "delta": ["1m", "5m", "15m", "30m", "60m", "4h", "1d", "1wk"],
    "zerodha": ["1m", "5m", "15m", "30m", "60m", "1d"],
}

# Available everywhere as a resample of stored daily bars, even where not
# native to the source itself.
_DERIVABLE_FROM_DAILY = ["1wk", "1mo"]


@dataclass
class TimeframeOption:
    value: str
    native: bool


def timeframes_for_source(source: str) -> list[TimeframeOption]:
    native = _NATIVE.get(source, [])
    options = [TimeframeOption(value=tf, native=True) for tf in native]
    native_set = set(native)
    for tf in _DERIVABLE_FROM_DAILY:
        if tf not in native_set:
            options.append(TimeframeOption(value=tf, native=False))
    order = {"1m": 0, "5m": 1, "15m": 2, "30m": 3, "60m": 4, "4h": 5, "1d": 6, "1wk": 7, "1mo": 8}
    return sorted(options, key=lambda o: order.get(o.value, 99))
