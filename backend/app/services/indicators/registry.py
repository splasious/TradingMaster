from app.services.indicators import momentum, structure, trend, volatility, volume
from app.services.indicators.base import IndicatorSpec

INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
    "sma": IndicatorSpec("sma", "Simple Moving Average", "trend", ["sma"], {"period": 20}, trend.sma),
    "ema": IndicatorSpec("ema", "Exponential Moving Average", "trend", ["ema"], {"period": 20}, trend.ema),
    "vwap": IndicatorSpec("vwap", "VWAP", "trend", ["vwap"], {}, trend.vwap),
    "supertrend": IndicatorSpec(
        "supertrend", "SuperTrend", "trend", ["supertrend", "trend"], {"period": 10, "multiplier": 3.0}, trend.supertrend
    ),
    "rsi": IndicatorSpec("rsi", "RSI", "momentum", ["rsi"], {"period": 14}, momentum.rsi),
    "macd": IndicatorSpec(
        "macd", "MACD", "momentum", ["macd", "signal", "histogram"],
        {"fast": 12, "slow": 26, "signal": 9}, momentum.macd,
    ),
    "stochastic": IndicatorSpec(
        "stochastic", "Stochastic Oscillator", "momentum", ["percent_k", "percent_d"],
        {"k_period": 14, "d_period": 3}, momentum.stochastic,
    ),
    "obv": IndicatorSpec("obv", "On-Balance Volume", "volume", ["obv"], {}, volume.obv),
    "mfi": IndicatorSpec("mfi", "Money Flow Index", "volume", ["mfi"], {"period": 14}, volume.mfi),
    "atr": IndicatorSpec("atr", "Average True Range", "volatility", ["atr"], {"period": 14}, volatility.atr),
    "bollinger_bands": IndicatorSpec(
        "bollinger_bands", "Bollinger Bands", "volatility", ["upper", "middle", "lower"],
        {"period": 20, "std_dev": 2.0}, volatility.bollinger_bands,
    ),
    "pivot_points": IndicatorSpec(
        "pivot_points", "Pivot Points", "structure", ["pivot", "r1", "s1", "r2", "s2"], {}, structure.pivot_points
    ),
    "swing_high_low": IndicatorSpec(
        "swing_high_low", "Swing High/Low", "structure", ["swing_high", "swing_low"], {"window": 5},
        structure.swing_high_low,
    ),
}


def get_indicator(code: str) -> IndicatorSpec:
    spec = INDICATOR_REGISTRY.get(code)
    if spec is None:
        raise ValueError(f"Unknown indicator '{code}'")
    return spec
