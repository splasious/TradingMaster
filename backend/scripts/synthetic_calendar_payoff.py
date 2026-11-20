"""Synthetic Calendar 15 MIN and its Reverse: what each open position makes or
loses by its close -- 15:00 on the weekly's expiry day -- across NIFTY levels,
and its greeks now (20 Nov). Read-only.

Each expiry is priced off its own forward, the one its CE and PE at the
position's strike imply now (put-call parity: F = K + CE - PE, at their
bid/ask midpoints -- what the app's other payoff model leaves out, rates and
dividends, is all a same-strike CE/PE pair carries), Black-76 at each leg's
own implied volatility then (held as it is):

  the weekly at 15:00 on its expiry day: its intrinsic value (half an hour
              before it settles);
  the monthly then: at its forward then, NIFTY then grown at the yearly
              carry its forward implies now (ln(F / NIFTY) / years left),
              for the years it has left then.

Closed as the strategy closes: a sold leg bought back at that value plus half
its bid/ask spread now, a bought leg sold at it less half, less the closing
orders' charges and the opening ones already paid. Greeks are the whole
position's, per unit, by repricing now: NIFTY +-1 point (delta, gamma),
every volatility +1 point (vega), a day on (theta), and the monthly's
yearly carry +0.25% (carry).

The public log gets points per unit (a unit: one of the 1,300 a leg); the
rupees, those times each leg's quantity, go to the private Telegram chat.
Run inside the backend container (the health check's `synthetic_payoff`
input pipes it to `python -`).
"""

import asyncio
import json
import math
import traceback
import urllib.request
from datetime import date, datetime, time, timezone

from sqlalchemy import func, select

from app.core.security import create_access_token
from app.db.session import AsyncSessionLocal
from app.models.paper_trading import PaperNativeDeployment
from app.models.strategy import Strategy
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram
from app.services.options.calendar_payoff import implied_vol, option_value
from app.services.paper_trading.trade_record import order_charges

NAMES = ("Synthetic Calendar 15 MIN", "Synthetic Calendar Reverse 15 MIN")
EXPIRY_TIME, CLOSE_TIME = time(15, 30), time(15, 0)
YEAR = 365.0 * 24 * 3600
GRID, STEP = 1000, 50
CARRY_BUMP = 0.0025


def years(expiry: date, at: datetime) -> float:
    return max((datetime.combine(expiry, EXPIRY_TIME, IST) - at).total_seconds() / YEAR, 1e-6)


def failed(where: str, exc: BaseException) -> None:
    lines = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename in ("<stdin>", __file__)]
    print(f"  {where}: not worked out ({type(exc).__name__} at line {lines[-1] if lines else '?'})")


def live_position(deployment_id, owner_id) -> dict:
    request = urllib.request.Request(
        f"http://localhost:8000/api/v1/paper-trading/native-deployments/{deployment_id}/position",
        headers={"Authorization": f"Bearer {create_access_token(str(owner_id), [])}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def charges(leg: dict, price: float, buying: bool) -> float:
    return order_charges({"option_type": leg["option_type"], "quantity": leg["quantity"]}, price, buying, False) or 0.0


def analyse(pos: dict, live: dict, now: datetime) -> tuple[list[str], list[str]]:
    legs = pos["legs"]
    weekly, monthly = date.fromisoformat(pos["weekly_expiry"]), date.fromisoformat(pos["monthly_expiry"])
    strike = float(pos["strike"])
    spot = float(live["underlying_price"])
    by_key = {(float(l["strike"]), l["option_type"], "sell" if l["side"] == "short" else "buy"): l for l in live["legs"]}
    book = {}
    for name, leg in legs.items():
        quote = by_key[(float(leg["strike"]), leg["option_type"], leg["side"])]
        bid, ask = quote.get("bid"), quote.get("ask")
        mid = (bid + ask) / 2 if bid and ask else float(quote.get("last_price") or quote["current_price"])
        book[name] = (mid, (ask - bid) / 2 if bid and ask else 0.0)
    which = {name: (weekly if leg["expiry"] == weekly.isoformat() else monthly) for name, leg in legs.items()}
    mid_of = lambda e, t: next(book[n][0] for n, l in legs.items() if which[n] == e and l["option_type"] == t)  # noqa: E731
    f_w = strike + mid_of(weekly, "CE") - mid_of(weekly, "PE")
    f_m = strike + mid_of(monthly, "CE") - mid_of(monthly, "PE")
    t_w, t_m = years(weekly, now), years(monthly, now)
    close_at = datetime.combine(weekly, CLOSE_TIME, IST)
    t_mh = years(monthly, close_at)
    carry_w, carry_m = math.log(f_w / spot) / t_w, math.log(f_m / spot) / t_m
    vols = {n: implied_vol(book[n][0], f_w if which[n] == weekly else f_m, strike, t_w if which[n] == weekly else t_m, l["option_type"])
            for n, l in legs.items()}
    units = next(iter(legs.values()))["quantity"]

    def price_now(n: str, s: float, vol_bump: float = 0.0, days: float = 0.0, carry_bump: float = 0.0) -> float:
        leg = legs[n]
        if which[n] == weekly:
            t, c = max(t_w - days / 365, 1e-6), carry_w
        else:
            t, c = max(t_m - days / 365, 1e-6), carry_m + carry_bump
        return option_value(s * math.exp(c * t), strike, t, vols[n] + vol_bump, leg["option_type"])

    def value(prices: dict[str, float]) -> float:  # per unit: what the legs are worth to the position
        return sum((1 if legs[n]["side"] == "buy" else -1) * p for n, p in prices.items())

    base = value({n: price_now(n, spot) for n in legs})
    delta = (value({n: price_now(n, spot + 1) for n in legs}) - value({n: price_now(n, spot - 1) for n in legs})) / 2
    gamma = value({n: price_now(n, spot + 1) for n in legs}) - 2 * base + value({n: price_now(n, spot - 1) for n in legs})
    vega = value({n: price_now(n, spot, vol_bump=0.01) for n in legs}) - base
    theta = value({n: price_now(n, spot, days=1) for n in legs}) - base
    carry = value({n: price_now(n, spot, carry_bump=CARRY_BUMP) for n in legs}) - base

    def at_close(s: float, carry_bump: float = 0.0) -> float:
        """Net per unit if all four close at 15:00 on the weekly's expiry day with NIFTY at `s`."""
        total = -pos["entry_charges"] / units
        for n, leg in legs.items():
            if which[n] == weekly:
                fair = max(s - strike, 0.0) if leg["option_type"] == "CE" else max(strike - s, 0.0)
            else:
                fair = option_value(s * math.exp((carry_m + carry_bump) * t_mh), strike, t_mh, vols[n], leg["option_type"])
            half = book[n][1]
            exit_price = max(fair + half, 0.05) if leg["side"] == "sell" else max(fair - half, 0.05)
            diff = leg["entry_price"] - exit_price if leg["side"] == "sell" else exit_price - leg["entry_price"]
            total += diff - charges(leg, exit_price, buying=leg["side"] == "sell") / units
        return total

    centre = round(spot / STEP) * STEP
    grid = [(s, at_close(s)) for s in range(int(centre - GRID), int(centre + GRID) + 1, STEP)]
    worst, best = min(grid, key=lambda p: p[1]), max(grid, key=lambda p: p[1])
    flat = at_close(spot)
    entry_net = sum((1 if l["side"] == "buy" else -1) * l["entry_price"] for l in legs.values())
    spreads = sum(h for _, h in book.values())
    public = [
        f"  strike {strike:,.0f}; NIFTY {spot:,.1f}; weekly {weekly:%d %b} ({t_w * 365:.1f} days), monthly {monthly:%d %b} ({t_m * 365:.1f} days); "
        f"closes {weekly:%d %b} {CLOSE_TIME:%H:%M}",
        f"  entry {'debit' if entry_net >= 0 else 'credit'} {abs(entry_net):,.2f} a unit; the four legs worth {value({n: book[n][0] for n in legs}):+,.2f} "
        f"at their midpoints now; half-spreads now {spreads:,.2f} a unit in all",
        f"  forwards now: weekly {f_w:,.1f} (carry {carry_w * 100:+.2f}% a year), monthly {f_m:,.1f} (carry {carry_m * 100:+.2f}% a year); "
        f"implied vols {', '.join(f'{n} {v * 100:.1f}%' for n, v in vols.items())}",
        f"  at the {weekly:%d %b} {CLOSE_TIME:%H:%M} close, per unit after the spread and all charges: NIFTY unchanged {flat:+,.2f}; "
        f"over NIFTY {grid[0][0]:,}-{grid[-1][0]:,}: best {best[1]:+,.2f} (at {best[0]:,}), worst {worst[1]:+,.2f} (at {worst[0]:,})",
        "  by level: " + ", ".join(f"{s:,} {p:+.2f}" for s, p in grid[:: max(1, len(grid) // 8)]),
        f"  the monthly's carry {CARRY_BUMP * 100:+.2f}% a year at the close: {at_close(spot, CARRY_BUMP) - flat:+,.2f} a unit; "
        f"{-CARRY_BUMP * 100:+.2f}%: {at_close(spot, -CARRY_BUMP) - flat:+,.2f}",
        f"  greeks now, per unit: delta {delta:+.4f} (points a NIFTY point), gamma {gamma:+.6f}, vega {vega:+.3f} (a vol point), "
        f"theta {theta:+.3f} (a day), carry {carry:+.3f} (a +0.25% year)",
        f"  the Rs 14,000 take-profit is {14_000 / units:,.2f} a unit after charges",
    ]
    private = [
        f"strike {strike:,.0f}, {units:,.0f} units a leg: at the {weekly:%d %b} 15:00 close after spread and charges -- NIFTY unchanged "
        f"Rs {flat * units:+,.0f}; best Rs {best[1] * units:+,.0f} (NIFTY {best[0]:,}), worst Rs {worst[1] * units:+,.0f} (NIFTY {worst[0]:,}); "
        f"carry +-0.25%: Rs {(at_close(spot, CARRY_BUMP) - flat) * units:+,.0f} / {(at_close(spot, -CARRY_BUMP) - flat) * units:+,.0f}; "
        f"delta Rs {delta * units:+,.1f} a NIFTY point, vega Rs {vega * units:+,.0f} a vol point, theta Rs {theta * units:+,.0f} a day",
    ]
    return public, private


async def main() -> None:
    now = datetime.now(timezone.utc).astimezone(IST)
    private: list[str] = []
    async with AsyncSessionLocal() as db:
        for name in NAMES:
            print(f"{name}:")
            rows = (await db.execute(
                select(PaperNativeDeployment, Strategy.owner_id).join(Strategy, Strategy.id == PaperNativeDeployment.strategy_id)
                .where(func.trim(Strategy.name) == name, PaperNativeDeployment.status == "active")
            )).all()
            for card, owner in rows:
                pos = (card.state or {}).get("position")
                if not pos:
                    print("  no open position")
                    continue
                try:
                    public, mine = analyse(pos, live_position(card.id, owner), now)
                    print("\n".join(public))
                    private += [f"{name}: {line}" for line in mine]
                except Exception as exc:  # noqa: BLE001
                    failed(name, exc)
    if private:
        await send_telegram("Synthetic Calendars: payoff at the weekly's close (private)", "\n\n".join(private))
        print("sent to the private Telegram chat: rupees, yes")


if __name__ == "__main__":
    asyncio.run(main())
