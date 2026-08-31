from app.services.indicators import momentum, structure, trend, volatility, volume
from app.services.indicators.base import IndicatorSpec

INDICATOR_REGISTRY: dict[str, IndicatorSpec] = {
    # -- Trend (mostly price-scale overlays) --
    "sma": IndicatorSpec("sma", "Simple Moving Average", "trend", ["sma"], {"period": 20}, trend.sma, overlay=True),
    "ema": IndicatorSpec("ema", "Exponential Moving Average", "trend", ["ema"], {"period": 20}, trend.ema, overlay=True),
    "wma": IndicatorSpec("wma", "Weighted Moving Average", "trend", ["wma"], {"period": 20}, trend.wma, overlay=True),
    "dema": IndicatorSpec("dema", "Double EMA", "trend", ["dema"], {"period": 20}, trend.dema, overlay=True),
    "tema": IndicatorSpec("tema", "Triple EMA", "trend", ["tema"], {"period": 20}, trend.tema, overlay=True),
    "hma": IndicatorSpec("hma", "Hull Moving Average", "trend", ["hma"], {"period": 20}, trend.hma, overlay=True),
    "vwap": IndicatorSpec("vwap", "VWAP", "trend", ["vwap"], {}, trend.vwap, overlay=True),
    "supertrend": IndicatorSpec(
        "supertrend", "SuperTrend", "trend", ["supertrend", "trend"], {"period": 10, "multiplier": 3.0}, trend.supertrend,
        overlay=True,
    ),
    "parabolic_sar": IndicatorSpec(
        "parabolic_sar", "Parabolic SAR", "trend", ["sar"],
        {"af_start": 0.02, "af_increment": 0.02, "af_max": 0.2}, trend.parabolic_sar, overlay=True,
    ),
    "ichimoku": IndicatorSpec(
        "ichimoku", "Ichimoku Cloud", "trend", ["tenkan_sen", "kijun_sen", "senkou_span_a", "senkou_span_b"],
        {"tenkan_period": 9, "kijun_period": 26, "senkou_b_period": 52}, trend.ichimoku, overlay=True,
    ),
    "adx": IndicatorSpec(
        "adx", "Average Directional Index", "trend", ["adx", "plus_di", "minus_di"], {"period": 14}, trend.adx,
        overlay=False,
    ),
    "aroon": IndicatorSpec("aroon", "Aroon", "trend", ["aroon_up", "aroon_down"], {"period": 25}, trend.aroon, overlay=False),
    # -- Momentum (0-100 or unbounded oscillators -- own panel) --
    "rsi": IndicatorSpec("rsi", "RSI", "momentum", ["rsi"], {"period": 14}, momentum.rsi, overlay=False),
    "macd": IndicatorSpec(
        "macd", "MACD", "momentum", ["macd", "signal", "histogram"],
        {"fast": 12, "slow": 26, "signal": 9}, momentum.macd, overlay=False,
    ),
    "stochastic": IndicatorSpec(
        "stochastic", "Stochastic Oscillator", "momentum", ["percent_k", "percent_d"],
        {"k_period": 14, "d_period": 3}, momentum.stochastic, overlay=False,
    ),
    "cci": IndicatorSpec("cci", "Commodity Channel Index", "momentum", ["cci"], {"period": 20}, momentum.cci, overlay=False),
    "williams_r": IndicatorSpec(
        "williams_r", "Williams %R", "momentum", ["williams_r"], {"period": 14}, momentum.williams_r, overlay=False,
    ),
    "roc": IndicatorSpec("roc", "Rate of Change", "momentum", ["roc"], {"period": 12}, momentum.roc, overlay=False),
    "momentum": IndicatorSpec("momentum", "Momentum", "momentum", ["momentum"], {"period": 10}, momentum.momentum, overlay=False),
    "trix": IndicatorSpec("trix", "TRIX", "momentum", ["trix"], {"period": 15}, momentum.trix, overlay=False),
    "ultimate_oscillator": IndicatorSpec(
        "ultimate_oscillator", "Ultimate Oscillator", "momentum", ["ultimate_oscillator"],
        {"period1": 7, "period2": 14, "period3": 28}, momentum.ultimate_oscillator, overlay=False,
    ),
    "awesome_oscillator": IndicatorSpec(
        "awesome_oscillator", "Awesome Oscillator", "momentum", ["awesome_oscillator"], {"fast": 5, "slow": 34},
        momentum.awesome_oscillator, overlay=False,
    ),
    "cmo": IndicatorSpec("cmo", "Chande Momentum Oscillator", "momentum", ["cmo"], {"period": 14}, momentum.cmo, overlay=False),
    "ppo": IndicatorSpec(
        "ppo", "Percentage Price Oscillator", "momentum", ["ppo", "signal", "histogram"],
        {"fast": 12, "slow": 26, "signal": 9}, momentum.ppo, overlay=False,
    ),
    "dpo": IndicatorSpec("dpo", "Detrended Price Oscillator", "momentum", ["dpo"], {"period": 20}, momentum.dpo, overlay=False),
    # -- Volume (volume-derived scale -- own panel) --
    "obv": IndicatorSpec("obv", "On-Balance Volume", "volume", ["obv"], {}, volume.obv, overlay=False),
    "mfi": IndicatorSpec("mfi", "Money Flow Index", "volume", ["mfi"], {"period": 14}, volume.mfi, overlay=False),
    "accumulation_distribution": IndicatorSpec(
        "accumulation_distribution", "Accumulation/Distribution Line", "volume", ["adl"], {}, volume.accumulation_distribution,
        overlay=False,
    ),
    "chaikin_money_flow": IndicatorSpec(
        "chaikin_money_flow", "Chaikin Money Flow", "volume", ["chaikin_money_flow"], {"period": 20}, volume.chaikin_money_flow,
        overlay=False,
    ),
    "chaikin_oscillator": IndicatorSpec(
        "chaikin_oscillator", "Chaikin Oscillator", "volume", ["chaikin_oscillator"], {"fast": 3, "slow": 10},
        volume.chaikin_oscillator, overlay=False,
    ),
    "force_index": IndicatorSpec(
        "force_index", "Force Index", "volume", ["force_index"], {"period": 13}, volume.force_index, overlay=False,
    ),
    "ease_of_movement": IndicatorSpec(
        "ease_of_movement", "Ease of Movement", "volume", ["ease_of_movement"], {"period": 14}, volume.ease_of_movement,
        overlay=False,
    ),
    "vortex": IndicatorSpec(
        "vortex", "Vortex Indicator", "volume", ["vi_plus", "vi_minus"], {"period": 14}, volume.vortex, overlay=False,
    ),
    # -- Volatility (bands overlay price; scalar readings get their own panel) --
    "atr": IndicatorSpec("atr", "Average True Range", "volatility", ["atr"], {"period": 14}, volatility.atr, overlay=False),
    "bollinger_bands": IndicatorSpec(
        "bollinger_bands", "Bollinger Bands", "volatility", ["upper", "middle", "lower"],
        {"period": 20, "std_dev": 2.0}, volatility.bollinger_bands, overlay=True,
    ),
    "keltner_channels": IndicatorSpec(
        "keltner_channels", "Keltner Channels", "volatility", ["upper", "middle", "lower"],
        {"period": 20, "atr_period": 10, "multiplier": 2.0}, volatility.keltner_channels, overlay=True,
    ),
    "donchian_channels": IndicatorSpec(
        "donchian_channels", "Donchian Channels", "volatility", ["upper", "middle", "lower"], {"period": 20},
        volatility.donchian_channels, overlay=True,
    ),
    "std_dev": IndicatorSpec(
        "std_dev", "Standard Deviation", "volatility", ["std_dev"], {"period": 20}, volatility.std_dev, overlay=False,
    ),
    "historical_volatility": IndicatorSpec(
        "historical_volatility", "Historical Volatility", "volatility", ["historical_volatility"],
        {"period": 20, "trading_days": 252}, volatility.historical_volatility, overlay=False,
    ),
    "choppiness_index": IndicatorSpec(
        "choppiness_index", "Choppiness Index", "volatility", ["choppiness_index"], {"period": 14},
        volatility.choppiness_index, overlay=False,
    ),
    # -- Structure (chart-level price annotations) --
    "pivot_points": IndicatorSpec(
        "pivot_points", "Pivot Points", "structure", ["pivot", "r1", "s1", "r2", "s2"], {}, structure.pivot_points,
        overlay=True,
    ),
    "swing_high_low": IndicatorSpec(
        "swing_high_low", "Swing High/Low", "structure", ["swing_high", "swing_low"], {"window": 5},
        structure.swing_high_low, overlay=True,
    ),
}


def get_indicator(code: str) -> IndicatorSpec:
    spec = INDICATOR_REGISTRY.get(code)
    if spec is None:
        raise ValueError(f"Unknown indicator '{code}'")
    return spec
