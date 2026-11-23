"""OI Change Buy 15 MIN: a stop-loss with a step-up, optimised on the
2-candle average (24 Nov, "check the optimization on stop loss on gap of
100 rs starting from 500 to 2000 for best result with step up of 250 for
every 1000 rs"). Read-only.

Replays version 6's rule -- at each close 09:30-15:15 the side from the
average Put / Call dOI over the last 2 fifteen-minute candles (the session
before filling in at 09:30), the ATM weekly, 1 lot, a flip sold at the
candle's open and the other side bought at its own, out at 15:29 -- over
every whole NIFTY session the server has 15-minute change-in-OI records
for, priced on the stored 15-minute NIFTY option candles, bought at the
open + HALF_SPREAD and sold - HALF_SPREAD, charges on every order.

The stop under test, on the bid against the price paid (charges not
counted), agreed 24 Nov: a stop-loss of SL rupees a lot -- 500, 600, ...
2,000 -- then for every STEP_EVERY (Rs 1,000) a lot of profit the highest
bid since entry has reached, the stop moves up STEP_UP (Rs 250) a lot: a Rs
500 stop is at -250 once +1,000 has been seen, breakeven at +2,000, +250 at
+3,000. Each SL runs twice: the step-up stop alone, and together with the
+15% / 5% trailing stop (whichever stop is higher). After a stop: flat
until the next close, then that close's side. References: version 6 (the
trail, no stop-loss) and no stops at all.

A 15-minute candle has its high and low but not which came first, so every
run with a stop goes twice -- open -> high -> low -> close and open -> low
-> high -> close -- and reports the middle (both readings alongside).
The public log gets counts, % of the premium paid and points per unit,
and every trade of the best run (its lower reading); the rupees of 1 lot,
per run and per trade of the best, go to the private Telegram chat. Run
inside the backend container (the health check's `oi_sl_optimise` input
pipes it to `python -`)."""

import asyncio
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.pcr import PcrSnapshot
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram
from app.services.paper_trading.trade_record import order_charges

FIRST_MARK, LAST_MARK = time(9, 30), time(15, 15)
CANDLES_FROM, CANDLES_TO = time(9, 30), time(15, 30)  # records closing a trading candle
MARK_STEP = timedelta(minutes=15)
STRIKE_STEP = 50
HALF_SPREAD = 0.001
DEFAULT_LOT = 65
TRAIL_START, TRAIL = 0.15, 0.05
PATHS = {"high first": ("open", "high", "low", "close"), "low first": ("open", "low", "high", "close")}
WINDOW = 2
STEP_EVERY, STEP_UP = 1_000.0, 250.0  # rupees a lot
SL_GRID = tuple(range(500, 2001, 100))
VARIANTS = (  # name, window, stop-loss (rupees a lot, with the step-up) or None, trail
    ("v6: trail, no stop-loss", WINDOW, None, True),
    ("no stops", WINDOW, None, False),
    *((f"SL {sl:,} + step-up", WINDOW, float(sl), False) for sl in SL_GRID),
    *((f"SL {sl:,} + step-up + trail", WINDOW, float(sl), True) for sl in SL_GRID),
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


async def simulate(data: Data, day: date, candles, index, records, window: int, stop_loss: float | None, trail: bool, order: str):
    trades, no_price, pos = [], 0, None

    def book(exit_price: float, reason: str, at: datetime) -> None:
        nonlocal pos
        qty, entry, option_type = pos["qty"], pos["entry"], pos["key"][2]
        cost = charges(option_type, qty, entry, True) + charges(option_type, qty, exit_price, False)
        net = (exit_price - entry) * qty - cost
        trades.append({"day": day, "opened": pos["opened"], "closed": at, "entry": entry, "exit": exit_price, "qty": qty,
                       "option": f"{pos['key'][0]:%d %b} {pos['key'][1]:,.0f} {option_type}",
                       "reason": reason, "net": net, "points": net / qty, "pct": net / (entry * qty) * 100})
        pos = None

    async def price_at(key, m: datetime, field: str = "open") -> float | None:
        bars = await data.day_bars(key, day)
        bar = bars.get(m.time())
        if bar is not None:
            return bar[field]
        earlier = [t for t in bars if t < m.time()]
        return bars[max(earlier)]["close"] if earlier else None

    for m in marks_of(day):
        rec = records.get(m.time())
        side = side_at(candles, index, m, window)
        if pos is not None and side is not None and side != pos["key"][2]:
            price = await price_at(pos["key"], m)
            if price is not None:
                book(bid(price), "oi_flip", m)
        if pos is None and side is not None and rec is not None and rec.spot:
            expiries = [e for e in (date.fromisoformat(x) for x in (rec.expiries or [])) if e >= day]
            weekly = (expiries[1] if expiries[0] == day and len(expiries) > 1 else expiries[0]) if expiries else None
            key = (weekly, atm(rec.spot), side)
            bar = (await data.day_bars(key, day)).get(m.time()) if weekly is not None else None
            if bar is None:
                no_price += 1
            else:
                entry, qty = ask(bar["open"]), float(data.contracts[key][1])
                pos = {"key": key, "entry": entry, "qty": qty, "opened": m, "peak": bid(bar["open"]), "stop": None}
        if pos is not None and (stop_loss is not None or trail):
            bar = (await data.day_bars(pos["key"], day)).get(m.time())
            if bar is not None:
                for i, field in enumerate(PATHS[order]):
                    p = bid(bar[field])
                    pos["peak"] = max(pos["peak"], p)
                    if trail and pos["stop"] is None and pos["peak"] >= pos["entry"] * (1 + TRAIL_START):
                        pos["stop"] = pos["peak"] * (1 - TRAIL)
                    if pos["stop"] is not None:
                        pos["stop"] = max(pos["stop"], pos["peak"] * (1 - TRAIL))
                    step = None
                    if stop_loss is not None:  # the stop-loss, up STEP_UP for every STEP_EVERY the best bid has made
                        steps = max(0, int((pos["peak"] - pos["entry"]) * pos["qty"] // STEP_EVERY))
                        step = pos["entry"] - (stop_loss - STEP_UP * steps) / pos["qty"]
                    stops = [(v, why) for v, why in ((pos["stop"], "trailing_stop"), (step, "stop_loss")) if v is not None]
                    if stops:
                        level, why = max(stops)
                        if p <= level:
                            book(p if i == 0 else level, why, m + MARK_STEP / 2)
                            break
    if pos is not None:
        price = await price_at(pos["key"], datetime.combine(day, LAST_MARK, tzinfo=IST), "close")
        book(bid(price if price is not None else pos["entry"]), "time_cutoff_329pm", datetime.combine(day, time(15, 29), tzinfo=IST))
    return trades, no_price


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
        "flips": sum(t["reason"] == "oi_flip" for t in trades), "stops": sum(t["reason"] in ("stop_loss", "trailing_stop") for t in trades),
        "sl_exits": sum(t["reason"] == "stop_loss" for t in trades),
        "by_day": by_day, "points_day": points_day,
        "avg_hold": statistics.mean([(t["closed"] - t["opened"]).total_seconds() / 60 for t in trades]) if trades else 0.0,
    }


def middle(a: dict, b: dict) -> dict:
    out = {k: (a[k] + b[k]) / 2 for k in a if isinstance(a[k], (int, float))}
    out["by_day"] = {d: (a["by_day"].get(d, 0.0) + b["by_day"].get(d, 0.0)) / 2 for d in set(a["by_day"]) | set(b["by_day"])}
    out["points_day"] = {d: (a["points_day"].get(d, 0.0) + b["points_day"].get(d, 0.0)) / 2
                         for d in set(a["points_day"]) | set(b["points_day"])}
    return out


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
        today = datetime.now(timezone.utc).astimezone(IST).date()
        days = [d for d in sorted(records) if d < today or datetime.now(timezone.utc).astimezone(IST).time() > time(15, 30)]
        days = days[1:]  # the first stored session only starts the averages
        print(f"sessions: {len(days)} ({days[0]} -> {days[-1]}); candles with a 15-min change: {len(candles)}; "
              f"NIFTY option contracts stored: {len(data.contracts)}")

        results = []
        for name, window, stop_loss, trail in VARIANTS:
            orders = list(PATHS) if (stop_loss is not None or trail) else ["high first"]
            runs, missing = {}, 0
            for order in orders:
                trades = []
                for day in days:
                    got, no_price = await simulate(data, day, candles, index, records[day], window, stop_loss, trail, order)
                    trades += got
                    missing += no_price if order == orders[0] else 0
                runs[order] = summary(trades, days)
                runs[order]["trade_list"] = trades
            mid = middle(*runs.values()) if len(runs) == 2 else next(iter(runs.values()))
            results.append({"name": name, "window": window, "trail": trail, "stop_loss": stop_loss, "mid": mid, "runs": runs,
                            "missing": missing})

    print("1 lot, net of the 0.1% half-spread and charges; points per unit summed over every trade; the middle of the two "
          "candle orders where a variant has stops (the two readings in brackets):")
    print(f"  {'variant':<40} {'points':>8} {'net% prem':>9} {'1st half':>8} {'2nd half':>8} {'win%':>5} {'trades':>6} "
          f"{'flips':>5} {'stops':>5} {'SL hits':>7} {'days up':>8} {'avg hold':>8}")
    for r in results:
        m = r["mid"]
        span = ""
        if len(r["runs"]) == 2:
            a, b = r["runs"].values()
            span = f"  ({a['points']:+.1f} .. {b['points']:+.1f})"
        print(f"  {r['name']:<40} {m['points']:>+8.1f} {m['on_premium']:>+9.2f} {m['first_half_pts']:>+8.1f} "
              f"{m['second_half_pts']:>+8.1f} {m['win']:>5.1f} {m['trades']:>6.0f} {m['flips']:>5.0f} {m['stops']:>5.0f} "
              f"{m['sl_exits']:>7.0f} {m['days_up']:>4.0f}/{len(days):<3} {m['avg_hold']:>6.0f}m{span}")
    print(f"  entries passed over for want of a candle: {', '.join(str(r['missing']) for r in results)}")

    best_step = max((r for r in results if r["stop_loss"] is not None and not r["trail"]), key=lambda r: r["mid"]["points"])
    best_both = max((r for r in results if r["stop_loss"] is not None and r["trail"]), key=lambda r: r["mid"]["points"])
    print(f"best with the step-up alone: {best_step['name']} {best_step['mid']['points']:+.1f}; with the trail too: "
          f"{best_both['name']} {best_both['mid']['points']:+.1f}; v6 {results[0]['mid']['points']:+.1f}")
    picks = []
    for r in (max((best_step, best_both), key=lambda r: r["mid"]["points"]),):
        run = min(r["runs"].values(), key=lambda x: x["points"])  # the lower reading where there are two
        picks.append((r, run))
        print(f"every trade, {r['name']}{' (the lower of its two readings)' if len(r['runs']) == 2 else ''}: "
              f"{len(run['trade_list'])} trades, {run['points']:+.1f} points per unit")
        print("  T,day,opened,closed,option,bought,sold,points per unit net,exit")
        for t in run["trade_list"]:
            print(f"  T,{t['day']:%d %b},{t['opened']:%H:%M},{t['closed'].astimezone(IST):%H:%M},{t['option']},{t['entry']:.2f},"
                  f"{t['exit']:.2f},{t['points']:+.2f},{t['reason']}")

    lines = []
    for r in results:
        m = r["mid"]
        span = ""
        if len(r["runs"]) == 2:
            a, b = r["runs"].values()
            span = f" ({a['net']:+,.0f} / {b['net']:+,.0f})"
        lines.append(f"{r['name']}: Rs {m['net']:+,.0f}{span}; halves {m['first_half']:+,.0f} / {m['second_half']:+,.0f}; "
                     f"{m['trades']:.0f} trades, win {m['win']:.0f}%; max drawdown {m['drawdown']:,.0f}")
    await send_telegram("OI Change Buy 15 MIN: stop-loss Rs 500-2,000 with a Rs 250 step-up per Rs 1,000, on the 2-candle average",
                        f"{len(days)} sessions ({days[0]:%d %b} - {days[-1]:%d %b}), 1 lot, stored 15-min candles, 0.1% half-spread "
                        f"and charges; with stops, the middle of the two candle orders (both in brackets).\n\n" + "\n".join(lines))
    for r, run in picks:
        rows = [f"{t['day']:%d %b} {t['opened']:%H:%M}-{t['closed'].astimezone(IST):%H:%M} {t['option']}: "
                f"{t['entry']:.2f} -> {t['exit']:.2f}, Rs {t['net']:+,.0f} ({t['reason']})" for t in run["trade_list"]]
        await send_telegram(f"OI Change Buy 15 MIN: every trade, {r['name']}",
                            f"{len(rows)} trades, Rs {sum(t['net'] for t in run['trade_list']):+,.0f}"
                            f"{' (the lower of its two readings)' if len(r['runs']) == 2 else ''}\n\n" + "\n".join(rows))
    print("sent to the private Telegram chat: rupees per variant and per trade, yes")


if __name__ == "__main__":
    asyncio.run(main())
