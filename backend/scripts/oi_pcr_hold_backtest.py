"""OI Change Buy 15 MIN version 7: no trailing stop while PCR is 0.8-1.2 -- held to the OI flip (26 Nov). Read-only.

Asked 26 Nov: "see if the PCR range in between of .8 to 1.2 if CE and PE
hold till cross over without any trailing stoploss with improve the
result". Version 7 replayed over every whole NIFTY session with 15-minute
change-in-OI records, priced on the stored 15-minute NIFTY option candles
(as oi_overnight_backtest.py's D): at each close 09:30-15:15 the side from
the last 2 candles' average Put / Call dOI; the ATM weekly, 1 lot, bought at
the candle's open + HALF_SPREAD, sold - HALF_SPREAD, charges on every order;
a flip sold at the candle's open and the other side bought at its own; held
overnight; on its expiry day rolled at 12:00 to next week's ATM on the same
side; the +15% / 5% trail on the bid. Variants:

  v7         as live: the trail always on
  no trail   the trail never on: out on a flip or the roll (for reference)
  at entry   a trade bought while PCR (the 15-minute record's, at the close
             it was bought at) is in the range has no trail -- held to the
             flip or the roll; any other trade has the trail as now
  live       the trail paused while the latest 15-minute record's PCR is in
             the range and working while it's outside it; the highest bid
             since the buy keeps counting while paused, so when PCR leaves
             the range the stop is 5% under it -- an option already under
             it is sold at that candle's open

each with PCR 0.8-1.2 (asked) and, to see whether the edges matter, 0.9-1.1
and 0.7-1.3. A 15-minute candle has its high and low but not which came
first, so each runs twice -- open -> high -> low -> close and open -> low ->
high -> close -- and reports the middle. Public log: counts and points per
unit only (rupees: points x the lot). Run inside the backend container (the
health check's `oi_pcr_backtest` input pipes it to `python -`).
"""

import asyncio
import statistics
import traceback
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.pcr import PcrSnapshot
from app.services.broker.zerodha_broker import IST
from app.services.paper_trading.trade_record import order_charges

FIRST_MARK, LAST_MARK = time(9, 30), time(15, 15)
CANDLES_FROM, CANDLES_TO = time(9, 30), time(15, 30)  # records closing a trading candle
MARK_STEP = timedelta(minutes=15)
STRIKE_STEP = 50
HALF_SPREAD = 0.001
DEFAULT_LOT = 65
TRAIL_START, TRAIL = 0.15, 0.05
PATHS = {"high first": ("open", "high", "low", "close"), "low first": ("open", "low", "high", "close")}
WINDOW = 2  # version 6
ROLL_AT = time(12, 0)  # D: the roll on the held option's expiry day
RANGES = ((0.8, 1.2), (0.9, 1.1), (0.7, 1.3))
VARIANTS = (  # name, carry, trail: True / False / ("entry" | "live", low, high)
    ("v7: trail always on (live)", "roll_noon", True),
    ("v7 with no trail at all", "roll_noon", False),
    *((f"no trail if PCR {lo:g}-{hi:g} at entry", "roll_noon", ("entry", lo, hi)) for lo, hi in RANGES),
    *((f"trail paused while PCR {lo:g}-{hi:g}", "roll_noon", ("live", lo, hi)) for lo, hi in RANGES),
)


def marks_of(day: date) -> list[datetime]:
    out, m = [], datetime.combine(day, FIRST_MARK, tzinfo=IST)
    while m <= datetime.combine(day, LAST_MARK, tzinfo=IST):
        out.append(m)
        m += MARK_STEP
    return out


def atm(spot: float) -> float:
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def bid(price: float) -> float:
    return price * (1 - HALF_SPREAD)


def ask(price: float) -> float:
    return price * (1 + HALF_SPREAD)


def charges(option_type: str, qty: float, price: float, buying: bool) -> float:
    return order_charges({"option_type": option_type, "quantity": qty}, price, buying, False) or 0.0


class Data:
    def __init__(self, db):
        self.db = db
        self.contracts: dict[tuple[date, float, str], tuple] = {}
        self.bars: dict[tuple, dict[time, dict]] = {}

    async def load_contracts(self) -> None:
        rows = (await self.db.execute(
            select(BfSymbol.id, BfSymbol.expiry, BfSymbol.strike, BfSymbol.option_type, BfSymbol.lot_size)
            .where(BfSymbol.underlying_symbol == "NIFTY", BfSymbol.option_type.is_not(None))
        )).all()
        for sid, expiry, strike, option_type, lot in rows:
            if expiry is not None and strike is not None:
                self.contracts[(expiry, float(strike), option_type)] = (sid, lot or DEFAULT_LOT)

    async def day_bars(self, key, day: date) -> dict[time, dict]:
        if (key, day) in self.bars:
            return self.bars[(key, day)]
        found = self.contracts.get(key)
        bars: dict[time, dict] = {}
        if found is not None:
            start = datetime.combine(day, time(9, 0), tzinfo=IST).astimezone(timezone.utc)
            rows = (await self.db.execute(
                select(BfOhlcvBar.ts, BfOhlcvBar.open, BfOhlcvBar.high, BfOhlcvBar.low, BfOhlcvBar.close)
                .where(BfOhlcvBar.symbol_id == found[0], BfOhlcvBar.timeframe == "15m",
                       BfOhlcvBar.ts >= start, BfOhlcvBar.ts < start + timedelta(hours=7))
            )).all()
            for ts, o, h, lo, c in rows:
                if None not in (o, h, lo, c):
                    bars[as_aware_utc(ts).astimezone(IST).time()] = {"open": float(o), "high": float(h), "low": float(lo),
                                                                     "close": float(c)}
        self.bars[(key, day)] = bars
        return bars


def side_at(candles: list[PcrSnapshot], index: dict[datetime, int], m: datetime, window: int) -> str | None:
    """The side at close `m`: the last `window` candles' average Put dOI against the average Call dOI."""
    i = index.get(m)
    if i is None:
        return None  # no record at this close: passed over, what's held kept
    last = candles[max(0, i - window + 1): i + 1]
    put = sum(c.put_oi_change for c in last) / len(last)
    call = sum(c.call_oi_change for c in last) / len(last)
    return None if put == call else ("CE" if put > call else "PE")


def summary(trades: list[dict], days: list[date]) -> dict:
    by_day = defaultdict(float)
    points_day = defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["net"]
        points_day[t["day"]] += t["points"]
    equity = peak = drawdown = 0.0
    for d in days:
        equity += by_day.get(d, 0.0)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    premium = sum(t["entry"] * t["qty"] for t in trades)
    half = set(days[: len(days) // 2])
    return {
        "trades": len(trades), "win": sum(t["net"] > 0 for t in trades) / len(trades) * 100 if trades else 0.0,
        "net": sum(by_day.values()), "premium": premium, "points": sum(points_day.values()),
        "on_premium": sum(by_day.values()) / premium * 100 if premium else 0.0,
        "days_up": sum(by_day.get(d, 0.0) > 0 for d in days), "drawdown": drawdown,
        "first_half": sum(v for d, v in by_day.items() if d in half), "second_half": sum(v for d, v in by_day.items() if d not in half),
        "first_half_pts": sum(v for d, v in points_day.items() if d in half),
        "second_half_pts": sum(v for d, v in points_day.items() if d not in half),
        "flips": sum(t["reason"] == "oi_flip" for t in trades), "stops": sum(t["reason"] in ("stop_loss", "trailing_stop", "trailing_stop_at_open") for t in trades),
        "by_day": by_day, "points_day": points_day,
        "avg_hold": statistics.mean([(t["closed"] - t["opened"]).total_seconds() / 60 for t in trades]) if trades else 0.0,
    }


def middle(a: dict, b: dict) -> dict:
    out = {k: (a[k] + b[k]) / 2 for k in a if isinstance(a[k], (int, float))}
    out["by_day"] = {d: (a["by_day"].get(d, 0.0) + b["by_day"].get(d, 0.0)) / 2 for d in set(a["by_day"]) | set(b["by_day"])}
    out["points_day"] = {d: (a["points_day"].get(d, 0.0) + b["points_day"].get(d, 0.0)) / 2
                         for d in set(a["points_day"]) | set(b["points_day"])}
    return out



async def run(data: Data, days: list[date], candles, index, records, carry: str, order: str, trail: bool = True):
    """(trades, overnight gaps, entries passed over) for one candle order."""
    trades, gaps, no_price, pos = [], [], 0, None
    reenter = trail == "reenter"
    pcr_now = {"pcr": None, "paused": 0, "watched": 0}  # the latest record's PCR; candles held with the trail paused / watched

    def in_range(pcr: float | None) -> bool:
        return pcr is not None and trail[1] <= pcr <= trail[2]

    def trail_on() -> bool:
        if isinstance(trail, tuple):
            return not in_range(pos["pcr"] if trail[0] == "entry" else pcr_now["pcr"])
        return bool(trail)

    def book(exit_price: float, reason: str, at: datetime, day: date) -> None:
        nonlocal pos
        qty, entry, option_type = pos["qty"], pos["entry"], pos["key"][2]
        cost = charges(option_type, qty, entry, True) + charges(option_type, qty, exit_price, False)
        net = (exit_price - entry) * qty - cost
        trades.append({"day": day, "opened": pos["opened"], "closed": at, "entry": entry, "exit": exit_price, "qty": qty,
                       "option": f"{pos['key'][0]:%d %b} {pos['key'][1]:,.0f} {option_type}", "reason": reason, "net": net,
                       "points": net / qty, "pct": net / (entry * qty) * 100, "nights": pos["nights"],
                       "rebought": pos.get("rebought", False), "exit_vs_paid": exit_price - entry,
                       "pcr_in": pos.get("pcr"), "no_trail": isinstance(trail, tuple) and trail[0] == "entry" and in_range(pos.get("pcr"))})
        pos = None

    async def price_at(key, day: date, t: time, field: str = "open") -> float | None:
        bars = await data.day_bars(key, day)
        bar = bars.get(t)
        if bar is not None:
            return bar[field]
        earlier = [x for x in bars if x < t]
        return bars[max(earlier)]["close"] if earlier else None

    async def walk(day: date, t: time) -> dict | None:
        """The trail along the held option's candle starting at `t`; what it sold, if it fired above the price paid."""
        if pos is None or trail is False:
            return None
        bar = (await data.day_bars(pos["key"], day)).get(t)
        if bar is None:
            return None
        on = trail_on()
        pcr_now["watched" if on else "paused"] += 1
        for i, field in enumerate(PATHS[order]):
            p = bid(bar[field])
            pos["peak"] = max(pos["peak"], p)
            if pos["stop"] is None and pos["peak"] >= pos["entry"] * (1 + TRAIL_START):
                pos["stop"] = pos["peak"] * (1 - TRAIL)
            if pos["stop"] is not None and on:
                pos["stop"] = max(pos["stop"], pos["peak"] * (1 - TRAIL))
                if p <= pos["stop"]:
                    at = datetime.combine(day, t, tzinfo=IST) + MARK_STEP / 2
                    fill = p if i == 0 else pos["stop"]
                    in_profit = fill > pos["entry"]
                    sold = {"key": pos["key"], "qty": pos["qty"], "fill": fill, "at": at if i else datetime.combine(day, t, tzinfo=IST)}
                    book(fill, "trailing_stop_at_open" if (i == 0 and t == time(9, 15)) else "trailing_stop", sold["at"], day)
                    return sold if in_profit else None
        return None

    def rebuy(sold: dict) -> None:
        """G: the same contract bought back where the trail sold it -- its bid there, plus the half-spread both ways."""
        nonlocal pos
        pos = {"key": sold["key"], "entry": ask(sold["fill"] / (1 - HALF_SPREAD)), "qty": sold["qty"], "opened": sold["at"],
               "peak": sold["fill"], "stop": None, "nights": 0, "rebought": True}

    previous_day = None
    for n_day, day in enumerate(days):
        next_day = days[n_day + 1] if n_day + 1 < len(days) else None
        if pos is not None:  # carried overnight (B): the gap, then the trail from 09:15
            open_price = await price_at(pos["key"], day, time(9, 15))
            if open_price is not None:
                gaps.append({"from": previous_day, "to": day, "option": f"{pos['key'][0]:%d %b} {pos['key'][1]:,.0f} {pos['key'][2]}",
                             "close": pos["carried_at"], "open": bid(open_price), "points": bid(open_price) - pos["carried_at"],
                             "weekend": (day - previous_day).days > 1, "expiry_days": (pos["key"][0] - day).days,
                             "in_profit": pos["carried_at"] > pos["entry"]})
            pos["nights"] += 1
            sold = await walk(day, time(9, 15))
            if sold and reenter:
                rebuy(sold)
        for m in marks_of(day):
            rec = records.get(day, {}).get(m.time())
            if rec is not None and rec.pcr is not None:
                pcr_now["pcr"] = float(rec.pcr)  # the PCR known from this close on
            side = side_at(candles, index, m, WINDOW)
            if pos is not None and side is not None and side != pos["key"][2]:
                price = await price_at(pos["key"], day, m.time())
                if price is not None:
                    book(bid(price), "oi_flip", m, day)
            rolled = None
            if carry == "roll_noon" and pos is not None and pos["key"][0] <= day and m.time() >= ROLL_AT:
                price = await price_at(pos["key"], day, m.time())
                if price is not None:
                    rolled = pos["key"][2]
                    book(bid(price), "expiry_rollover", m, day)
            side = side if side is not None else rolled
            if pos is None and side is not None and rec is not None and rec.spot:
                expiries = [e for e in (date.fromisoformat(x) for x in (rec.expiries or [])) if e >= day]
                weekly = (expiries[1] if expiries[0] == day and len(expiries) > 1 else expiries[0]) if expiries else None
                key = (weekly, atm(rec.spot), side)
                bar = (await data.day_bars(key, day)).get(m.time()) if weekly is not None else None
                if bar is None:
                    no_price += 1
                else:
                    pos = {"key": key, "entry": ask(bar["open"]), "qty": float(data.contracts[key][1]), "opened": m,
                           "peak": bid(bar["open"]), "stop": None, "nights": 0, "pcr": float(rec.pcr) if rec.pcr is not None else None}
            sold = await walk(day, m.time())
            if sold and reenter and pos is None:
                rebuy(sold)
        if pos is not None:
            close_price = await price_at(pos["key"], day, LAST_MARK, "close")
            close_bid = bid(close_price if close_price is not None else pos["entry"])
            if carry == "none" or next_day is None:
                book(close_bid, "time_cutoff_329pm", datetime.combine(day, time(15, 29), tzinfo=IST), day)
            elif pos["key"][0] <= day:
                book(close_bid, "expiry_day_329pm", datetime.combine(day, time(15, 29), tzinfo=IST), day)
            elif carry == "not_into_expiry" and pos["key"][0] <= next_day:
                book(close_bid, "before_expiry_329pm", datetime.combine(day, time(15, 29), tzinfo=IST), day)
            else:
                pos["carried_at"] = close_bid
        previous_day = day
    return trades, gaps, no_price, pcr_now


async def main() -> None:
    async with AsyncSessionLocal() as db:
        data = Data(db)
        await data.load_contracts()
        snaps = (await db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == "NIFTY").order_by(PcrSnapshot.ts))).scalars().all()
        records: dict[date, dict[time, PcrSnapshot]] = defaultdict(dict)
        candles: list[PcrSnapshot] = []
        for snap in snaps:
            at = as_aware_utc(snap.ts).astimezone(IST)
            if CANDLES_FROM <= at.time() <= CANDLES_TO and snap.put_oi_change is not None and snap.call_oi_change is not None:
                candles.append(snap)
                records[at.date()][at.time()] = snap
        index = {as_aware_utc(c.ts).astimezone(IST): i for i, c in enumerate(candles)}
        now = datetime.now(timezone.utc).astimezone(IST)
        days = [d for d in sorted(records) if d < now.date() or now.time() > time(15, 30)][1:]
        pcrs = [float(c.pcr) for c in candles if c.pcr is not None and as_aware_utc(c.ts).astimezone(IST).date() in set(days)]
        print("Rules: version 7 as live (2-candle average Put/Call dOI picks the side at each close 09:30-15:15; the ATM weekly")
        print("  bought at the candle's open; held overnight; rolled 12:00 on its expiry day; +15% / 5% trail on the bid), 1 lot,")
        print("  0.1% half-spread and charges; the trail changed only as each variant says (PCR: the 15-minute record's)")
        print(f"sessions: {len(days)} ({days[0]} -> {days[-1]}); candles with a 15-min change: {len(candles)}")
        if pcrs:
            pcrs.sort()
            print(f"PCR at the closes: median {pcrs[len(pcrs) // 2]:.3f}, lowest {pcrs[0]:.3f}, highest {pcrs[-1]:.3f}; in range: "
                  + ", ".join(f"{lo:g}-{hi:g} {100 * sum(lo <= p <= hi for p in pcrs) / len(pcrs):.0f}%" for lo, hi in RANGES))
        results = []
        for name, carry, trail in VARIANTS:
            runs = {}
            try:
                for order in PATHS:
                    trades, _gaps, missing, pcr_now = await run(data, days, candles, index, records, carry, order, trail)
                    runs[order] = {**summary(trades, days), "trade_list": trades, "missing": missing, "pcr_now": pcr_now}
            except Exception as exc:  # noqa: BLE001 -- the kind and this script's line only, never the message
                lines = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename in ("<stdin>", __file__)]
                print(f"  {name}: not run ({type(exc).__name__} at line {lines[-1] if lines else '?'})")
                continue
            results.append({"name": name, "trail": trail, "runs": runs, "mid": middle(*runs.values())})

    print()
    print("1 lot, net of the 0.1% half-spread and charges; points per unit summed; the middle of the two candle orders "
          "(both readings in brackets):")
    print(f"  {'variant':<36} {'points':>8} {'1st half':>8} {'2nd half':>8} {'win%':>5} {'trades':>6} {'trail':>5} "
          f"{'days up':>8} {'avg hold':>8}")
    for r in results:
        m, (a, b) = r["mid"], r["runs"].values()
        print(f"  {r['name']:<36} {m['points']:>+8.1f} {m['first_half_pts']:>+8.1f} {m['second_half_pts']:>+8.1f} "
              f"{m['win']:>5.1f} {m['trades']:>6.0f} {m['stops']:>5.0f} {m['days_up']:>4.0f}/{len(days):<3} {m['avg_hold']:>6.0f}m"
              f"  ({a['points']:+.1f} .. {b['points']:+.1f})")
    print()
    for r in results:
        if not isinstance(r["trail"], tuple):
            continue
        for order, run_ in r["runs"].items():
            tl = run_["trade_list"]
            if r["trail"][0] == "entry":
                off = [t for t in tl if t["no_trail"]]
                on = [t for t in tl if not t["no_trail"]]
                print(f"{r['name']}, {order}: {len(off)} trades with no trail ({sum(t['points'] for t in off):+.1f} pts, won "
                      f"{sum(t['points'] > 0 for t in off)}), {len(on)} with it ({sum(t['points'] for t in on):+.1f} pts)")
            else:
                c = run_["pcr_now"]
                print(f"{r['name']}, {order}: candles held with the trail paused {c['paused']}, working {c['watched']}; "
                      f"trail exits {sum(t['reason'].startswith('trailing_stop') for t in tl)}")
    base = results[0]
    print()
    print("v7's own trades split by the PCR at their buy (the middle of the two orders' sums):")
    for lo, hi in RANGES:
        parts = []
        for order, run_ in base["runs"].items():
            tl = run_["trade_list"]
            ins = [t for t in tl if t["pcr_in"] is not None and lo <= t["pcr_in"] <= hi]
            parts.append((len(ins), sum(t["points"] for t in ins), len(tl) - len(ins), sum(t["points"] for t in tl) - sum(t["points"] for t in ins)))
        n_in, p_in = (parts[0][0] + parts[1][0]) / 2, (parts[0][1] + parts[1][1]) / 2
        n_out, p_out = (parts[0][2] + parts[1][2]) / 2, (parts[0][3] + parts[1][3]) / 2
        print(f"  PCR {lo:g}-{hi:g} at the buy: {n_in:.0f} trades, {p_in:+.1f} pts; outside it: {n_out:.0f} trades, {p_out:+.1f} pts")
    print("points per unit by day (the middle), in the variants' order:")
    for d in days:
        print(f"  D,{d:%d %b}," + ",".join(f"{r['mid']['points_day'].get(d, 0.0):+.1f}" for r in results))


if __name__ == "__main__":
    asyncio.run(main())
