"""A calendar position's profit at its exit across NIFTY levels (5 Nov) -- for
the Conti Calendar 15 MIN card: the most it can make and lose by today's
daily close (EXIT_TIME, the strategy's own: 15:29 since 8 Nov, 15:00
before), and the NIFTY range it ends in profit.

A calendar's figures aren't fixed like a plain straddle's: what the bought
monthly options are still worth when it closes depends on volatility. So
each leg is repriced at the exit time for every NIFTY level within
GRID_POINTS of NIFTY now, by Black-Scholes at the implied volatility its own
bid/ask midpoint gives now (held as it is), and closed as the strategy
closes it: a sold leg bought back at that value plus half its bid/ask
spread now, a bought leg sold at it less half, less the charges of the
closing orders and of the opening ones already paid. Rates and dividends
are left out (each volatility is implied under the same model, so they
largely cancel over days).

Also Conti Calendar 400 15 MIN's double calendar (11 Nov): held overnight,
its horizon is ROLL_TIME (15:00) on the sold weeklies' expiry day, when they
roll, and every point carries what earlier rolls have booked (`booked`).

Display only -- the strategies' own take-profit reads the live bid / ask
(native_strategies/nifty_conti_calendar.py, nifty_conti_calendar_400.py).
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Callable

from app.core.time import as_aware_utc
from app.services.broker.zerodha_broker import IST
from app.services.strategy.native_strategies.nifty_conti_calendar import DAILY_CLOSE
from app.services.strategy.native_strategies.nifty_conti_calendar_400 import REGIME as DOUBLE, ROLL_TIME, booked_net

EXPIRY_TIME = time(15, 30)  # IST: when an NSE option expires
EXIT_TIME = DAILY_CLOSE  # IST: Conti Calendar 15 MIN's daily close, the horizon shown
YEAR_SECONDS = 365.0 * 24 * 3600
GRID_POINTS = 1000  # NIFTY levels this far either side of NIFTY now
GRID_STEP = 10
MIN_PRICE = 0.05  # NSE's tick: nothing trades below it
VOL_FLOOR, VOL_CEILING = 0.005, 5.0


@dataclass
class PayoffLeg:
    name: str
    option_type: str  # CE / PE
    strike: float
    expiry: date
    side: str  # "sell" / "buy", as opened
    quantity: float
    entry_price: float
    mid: float  # now: the bid/ask midpoint, else the last price
    half_spread: float  # now: half the bid/ask gap, 0 without a book


@dataclass
class Payoff:
    horizon: datetime
    spot: float
    points: list[tuple[float, float]]  # (NIFTY at the exit, profit after every charge)
    max_profit: float
    max_profit_at: float
    max_loss: float  # the worst within the grid
    max_loss_at: float
    loss_cap: float | None  # a calendar can't lose more than its net debit and the charges
    breakeven_low: float | None  # None: beyond the grid, or no profit anywhere
    breakeven_high: float | None
    target: float | None
    target_low: float | None
    target_high: float | None
    vols: dict[str, float] = field(default_factory=dict)
    booked: float = 0.0  # what earlier rolls booked, in every point (the double calendar)
    # Every NIFTY range in profit / at the target, low to high -- a double
    # calendar can have one at each strike; breakeven_* and target_* are the
    # one around the best point. A side None: past the grid.
    profit_zones: list[tuple[float | None, float | None]] = field(default_factory=list)
    target_zones: list[tuple[float | None, float | None]] = field(default_factory=list)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def option_value(spot: float, strike: float, years: float, vol: float, option_type: str) -> float:
    """Black-Scholes value with no rate or dividend; intrinsic at or after expiry."""
    intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
    if years <= 0 or vol <= 0:
        return intrinsic
    sd = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if option_type == "CE":
        return spot * _cdf(d1) - strike * _cdf(d2)
    return strike * _cdf(-d2) - spot * _cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, years: float, option_type: str) -> float:
    """The volatility at which option_value is `price` (bisection); the floor
    when the price holds no time value."""
    if years <= 0 or option_value(spot, strike, years, VOL_FLOOR, option_type) >= price:
        return VOL_FLOOR
    lo, hi = VOL_FLOOR, VOL_CEILING
    if option_value(spot, strike, years, hi, option_type) <= price:
        return hi
    for _ in range(100):
        mid = (lo + hi) / 2
        if option_value(spot, strike, years, mid, option_type) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def years_until(expiry: date, at: datetime) -> float:
    ends = datetime.combine(expiry, EXPIRY_TIME, tzinfo=IST)
    return max((ends - at.astimezone(timezone.utc)).total_seconds() / YEAR_SECONDS, 0.0)


def _crossing(a: tuple[float, float], b: tuple[float, float], level: float) -> float:
    (x0, y0), (x1, y1) = a, b
    return x0 if y1 == y0 else x0 + (level - y0) * (x1 - x0) / (y1 - y0)


def _zone(points: list[tuple[float, float]], peak: int, level: float) -> tuple[float | None, float | None] | None:
    """The NIFTY range around the peak where profit is at least `level`:
    (low, high), a side None when the range runs past the grid; None when
    even the peak is below it."""
    if points[peak][1] < level:
        return None
    lo = peak
    while lo > 0 and points[lo - 1][1] >= level:
        lo -= 1
    hi = peak
    while hi < len(points) - 1 and points[hi + 1][1] >= level:
        hi += 1
    low = _crossing(points[lo - 1], points[lo], level) if lo > 0 else None
    high = _crossing(points[hi], points[hi + 1], level) if hi < len(points) - 1 else None
    return low, high


def _zones_at(points: list[tuple[float, float]], level: float) -> list[tuple[float | None, float | None]]:
    """Every NIFTY range where profit is at least `level`, low to high."""
    zones, i = [], 0
    while i < len(points):
        if points[i][1] < level:
            i += 1
            continue
        j = i
        while j + 1 < len(points) and points[j + 1][1] >= level:
            j += 1
        low = _crossing(points[i - 1], points[i], level) if i > 0 else None
        high = _crossing(points[j], points[j + 1], level) if j < len(points) - 1 else None
        zones.append((low, high))
        i = j + 1
    return zones


def calendar_payoff(
    legs: list[PayoffLeg], spot: float, now: datetime, horizon: datetime, entry_charges: float,
    charges: Callable[[PayoffLeg, float, bool], float], target: float | None = None, booked: float = 0.0,
) -> Payoff | None:
    """`charges(leg, price, buying)`: one closing order's estimated charges.
    `booked`: what's already been booked (a double calendar's rolls),
    added to every point. None when the horizon has passed."""
    if horizon <= now or not legs:
        return None
    vols = {leg.name: implied_vol(leg.mid, spot, leg.strike, years_until(leg.expiry, now), leg.option_type) for leg in legs}
    left = {leg.name: years_until(leg.expiry, horizon) for leg in legs}
    centre = round(spot / GRID_STEP) * GRID_STEP
    points = []
    for level in range(int(centre - GRID_POINTS), int(centre + GRID_POINTS) + 1, GRID_STEP):
        total = booked - entry_charges
        for leg in legs:
            value = option_value(float(level), leg.strike, left[leg.name], vols[leg.name], leg.option_type)
            if leg.side == "sell":
                exit_price = max(value + leg.half_spread, MIN_PRICE)
                total += (leg.entry_price - exit_price) * leg.quantity - charges(leg, exit_price, True)
            else:
                exit_price = max(value - leg.half_spread, MIN_PRICE)
                total += (exit_price - leg.entry_price) * leg.quantity - charges(leg, exit_price, False)
        points.append((float(level), total))

    peak = max(range(len(points)), key=lambda i: points[i][1])
    trough = min(range(len(points)), key=lambda i: points[i][1])
    breakeven = _zone(points, peak, 0.0) or (None, None)
    zone = _zone(points, peak, target) if target is not None else None

    bought = {(leg.option_type, leg.strike) for leg in legs if leg.side == "buy"}
    covered = all((leg.option_type, leg.strike) in bought for leg in legs if leg.side == "sell")
    net_debit = sum(leg.entry_price * leg.quantity * (1 if leg.side == "buy" else -1) for leg in legs)
    entry_exit = sum(charges(leg, leg.entry_price, leg.side == "sell") for leg in legs)
    return Payoff(
        horizon=horizon, spot=spot, points=points,
        max_profit=points[peak][1], max_profit_at=points[peak][0], max_loss=points[trough][1], max_loss_at=points[trough][0],
        loss_cap=(net_debit + entry_charges + entry_exit - booked) if covered and net_debit > 0 else None,
        breakeven_low=breakeven[0], breakeven_high=breakeven[1],
        target=target, target_low=zone[0] if zone else None, target_high=zone[1] if zone else None,
        vols=vols, booked=booked,
        profit_zones=_zones_at(points, 0.0), target_zones=_zones_at(points, target) if target is not None else [],
    )


def position_payoff(position: dict, instruments: dict, prices: dict[str, float | None], spot: float | None, now: datetime) -> Payoff | None:
    """A calendar position's (native_strategies/nifty_conti_calendar.py's
    state["position"]) profit at today's EXIT_TIME -- each leg off the order
    book the ticker last saw, else its price in `prices` (leg name -> price)
    with no spread. `instruments`: leg name -> its Instrument. None for any
    other position, without NIFTY or a leg's price, or once EXIT_TIME has come.
    Shared by the card (endpoints/paper_native_trading.py) and the Telegram
    entry alert (notifications/trade_alerts.py)."""
    from app.services.market_data.live_price import LIVE_PRICE_MAX_AGE
    from app.services.market_data.tick_engine import tick_engine
    from app.services.paper_trading.trade_record import order_charges

    regime = position.get("regime")
    if regime not in ("calendar", DOUBLE) or spot is None or not isinstance(position.get("legs"), dict):
        return None
    at = as_aware_utc(now)
    if regime == DOUBLE:  # held overnight: at the sold weeklies' roll
        horizon = datetime.combine(date.fromisoformat(position["weekly_expiry"]), ROLL_TIME, tzinfo=IST)
    else:
        horizon = datetime.combine(at.astimezone(IST).date(), EXIT_TIME, tzinfo=IST)
    legs = []
    for name, leg in position["legs"].items():
        instrument, price = instruments.get(name), prices.get(name)
        if instrument is None or price is None or None in (instrument.expiry, instrument.strike, instrument.option_type):
            return None
        book = tick_engine.get_fresh_bid_ask(instrument.id, LIVE_PRICE_MAX_AGE, at)
        mid, half = ((book[0] + book[1]) / 2, (book[1] - book[0]) / 2) if book else (price, 0.0)
        legs.append(PayoffLeg(
            name=name, option_type=instrument.option_type, strike=float(instrument.strike), expiry=instrument.expiry,
            side=leg["side"], quantity=float(leg["quantity"]), entry_price=float(leg["entry_price"]), mid=mid, half_spread=half,
        ))

    def charges(leg: PayoffLeg, price: float, buying: bool) -> float:
        return order_charges({"option_type": leg.option_type, "quantity": leg.quantity}, price, buying, False) or 0.0

    target = position.get("take_profit")
    return calendar_payoff(legs, spot, at, horizon, float(position.get("entry_charges") or 0.0), charges,
                           float(target) if isinstance(target, (int, float)) else None,
                           booked_net(position) if regime == DOUBLE else 0.0)
