"""A calendar's profit at today's 15:00 across NIFTY levels
(services/options/calendar_payoff.py, 5 Nov): each leg repriced by
Black-Scholes at its implied volatility now, closed at bid/ask, after
every charge -- the most it can make and lose, and where it breaks even."""

from datetime import date, datetime

import pytest

from app.services.broker.zerodha_broker import IST
from app.services.options import calendar_payoff as cp

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
THREE = datetime(2026, 11, 5, 15, 0, tzinfo=IST)
WEEKLY, MONTHLY = date(2026, 11, 9), date(2026, 11, 23)
QTY = 1040.0


def test_the_implied_volatility_gives_back_the_price():
    years = cp.years_until(MONTHLY, NOW)
    assert years == pytest.approx((18 * 24 + 5.5) / (365 * 24), rel=1e-6)  # to 15:30 on 23 Nov
    for option_type in ("CE", "PE"):
        price = cp.option_value(24310.0, 24300.0, years, 0.142, option_type)
        assert cp.implied_vol(price, 24310.0, 24300.0, years, option_type) == pytest.approx(0.142, abs=1e-6)
    assert cp.implied_vol(5.0, 24310.0, 24300.0, years, "CE") == cp.VOL_FLOOR  # under intrinsic: no time value
    assert cp.option_value(24310.0, 24300.0, 0.0, 0.2, "CE") == 10.0 and cp.option_value(24310.0, 24300.0, 0.0, 0.2, "PE") == 0.0


def _legs(spot: float = 24300.0, vol_weekly: float = 0.13, vol_monthly: float = 0.14, half: float = 0.5, sold_at_vol: float = 0.15):
    """The weekly sold earlier at `sold_at_vol` (it has eased since: the position is up), the monthly bought at its mid now."""
    legs = []
    for name, option_type, side, expiry, vol in (
        ("short_ce", "CE", "sell", WEEKLY, vol_weekly), ("short_pe", "PE", "sell", WEEKLY, vol_weekly),
        ("long_ce", "CE", "buy", MONTHLY, vol_monthly), ("long_pe", "PE", "buy", MONTHLY, vol_monthly),
    ):
        years = cp.years_until(expiry, NOW)
        mid = cp.option_value(spot, 24300.0, years, vol, option_type)
        entry = (cp.option_value(spot, 24300.0, years, sold_at_vol, option_type) - half) if side == "sell" else mid + half
        legs.append(cp.PayoffLeg(name, option_type, 24300.0, expiry, side, QTY, entry, mid, half))
    return legs


def _charges(leg, price, buying):
    return 0.0005 * price * leg.quantity  # a stand-in schedule


def test_it_peaks_near_the_strike_and_breaks_even_either_side_by_1500():
    legs = _legs()
    payoff = cp.calendar_payoff(legs, 24300.0, NOW, THREE, entry_charges=500.0, charges=_charges, target=14_000.0)
    xs = [x for x, _ in payoff.points]
    assert xs[0] == 23300.0 and xs[-1] == 25300.0 and len(xs) == 201  # NIFTY now +-1,000, every 10
    assert abs(payoff.max_profit_at - 24300.0) <= 50  # a calendar makes most where it started
    assert payoff.max_profit > 0  # the weekly sold richer than it trades now
    assert payoff.breakeven_low < 24300.0 < payoff.breakeven_high
    at = dict(payoff.points)
    assert at[round(payoff.breakeven_low, -1) - 10] < 0 < at[round(payoff.breakeven_low, -1) + 20]
    assert payoff.max_loss < 0 and payoff.max_loss_at in (23300.0, 25300.0)  # worst at the far ends
    net_debit = sum(leg.entry_price * QTY * (1 if leg.side == "buy" else -1) for leg in legs)
    assert payoff.loss_cap == pytest.approx(net_debit + 500.0 + sum(_charges(leg, leg.entry_price, leg.side == "sell") for leg in legs))
    assert payoff.max_loss > -payoff.loss_cap  # within the cap
    assert set(payoff.vols) == {"short_ce", "short_pe", "long_ce", "long_pe"}
    assert payoff.vols["long_ce"] == pytest.approx(0.14, abs=1e-4)


def test_the_target_zone_and_a_day_with_no_profit_anywhere():
    legs = _legs()
    rich = cp.calendar_payoff(legs, 24300.0, NOW, THREE, 500.0, _charges, target=1_000.0)
    assert rich.target_low is not None and rich.breakeven_low < rich.target_low < 24300.0 < rich.target_high < rich.breakeven_high
    far = cp.calendar_payoff(legs, 24300.0, NOW, THREE, 500.0, _charges, target=10_000_000.0)
    assert (far.target_low, far.target_high) == (None, None)

    wide = _legs(sold_at_vol=0.13, half=5.0)  # nothing gained since the open, and a wide book to close in
    poor = cp.calendar_payoff(wide, 24300.0, NOW, THREE, 500.0, _charges, target=14_000.0)
    assert poor.max_profit < 0 and (poor.breakeven_low, poor.breakeven_high) == (None, None)

    assert cp.calendar_payoff(legs, 24300.0, THREE, THREE, 500.0, _charges) is None  # 15:00 has come
