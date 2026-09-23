import pytest

from app.services.strategy.native_strategies.nifty_pcr_multi_regime import (
    determine_regime,
    leg_specs_for,
    pcr_exit_band,
    round_to_nearest_100,
)


def test_determine_regime_with_hysteresis_buffers():
    assert determine_regime(1.0) == "sideways"
    assert determine_regime(0.80) == "sideways"
    assert determine_regime(1.20) == "sideways"
    assert determine_regime(1.30) == "bullish"
    assert determine_regime(0.70) == "bearish"
    assert determine_regime(0.78) == "buffer"
    assert determine_regime(1.22) == "buffer"


def test_pcr_exit_band_matches_each_regimes_exit_rule():
    assert pcr_exit_band("sideways") == (0.75, 1.25)  # exit if PCR > 1.25 or < 0.75
    assert pcr_exit_band("bullish") == (1.20, None)  # exit when PCR falls below 1.20
    assert pcr_exit_band("bearish") == (None, 0.80)  # exit when PCR rises above 0.80
    with pytest.raises(ValueError):
        pcr_exit_band("buffer")


def test_leg_specs_and_rounding():
    assert round_to_nearest_100(23450) == 23500  # ties round up, never banker's rounding
    assert round_to_nearest_100(23449) == 23400
    assert leg_specs_for("sideways", 23400) == {"short_ce": (23400, "CE", "sell"), "short_pe": (23400, "PE", "sell")}
    assert leg_specs_for("bullish", 23400) == {"short_pe": (23200, "PE", "sell"), "long_pe": (23000, "PE", "buy")}
    assert leg_specs_for("bearish", 23400) == {"short_ce": (23600, "CE", "sell"), "long_ce": (23800, "CE", "buy")}
