"""OI Change Buy 15 MIN version 7 with a stop-loss of Rs 500 to 2,000 a lot
(26 Nov, "backtest stop-loss 500 to 2000 on v7"). Read-only.

Version 7's rule over every whole NIFTY session the server has 15-minute
change-in-OI records for, priced on the stored 15-minute NIFTY option
candles: at each close 09:30-15:15 the side from the average Put / Call dOI
over the last 2 candles; the ATM weekly, 1 lot, bought at the candle's open +
HALF_SPREAD, sold - HALF_SPREAD, charges on every order; a flip sold at the
candle's open and the other side bought at its own; nothing sold at 15:29 --
carried overnight, watched again from the next 09:15 open; on the held
option's expiry day rolled to next week's at 12:00; the +15% / 5% trail.

The stop-loss under test, on the bid against the price paid (charges not
counted), SL rupees a lot -- 500, 600, ... 2,000 -- in two forms, each with
the trail (the higher stop wins):

  fixed     until the trail is on (version 5's rule at Rs 1,200);
  step-up   for every STEP_EVERY (Rs 1,000) a lot the best bid has made, the
            stop moves up STEP_UP (Rs 250) a lot (as tested 24 Nov).

26 Nov, after it: the stop-loss as a % of the price paid instead -- 10, 15,
20 and 25% -- with the trail, and without it (out on the stop, a flip or
the expiry roll only); PCT_RUN picks this set. A carried option opening
under its stop is sold at the 09:15 open. After a
stop: flat until the next close, then that close's side. A 15-minute candle
has its high and low but not which came first, so each runs twice -- open ->
high -> low -> close and open -> low -> high -> close -- and reports the
middle (both readings alongside). Public log: counts, % of premium and
points per unit; the rupees of 1 lot to the private Telegram chat. Run
inside the backend container (the health check's `oi_v7_sl_backtest` input
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
WINDOW = 2  # version 6
ROLL_AT = time(12, 0)  # D: the roll on the held option's expiry day
STEP_EVERY, STEP_UP = 1_000.0, 250.0  # rupees a lot
SL_GRID = tuple(range(500, 2001, 100))
PCT_RUN = True  # 26 Nov: the stop-loss as a % of the price paid (False: the rupee grid above)
PCT_GRID = (0.10, 0.15, 0.20, 0.25)
VARIANTS = (  # name, carry, trail, stop-loss (rupees a lot, or "15%" as a fraction), step-up
    ("v7 as live: trail, no stop-loss", "roll_noon", True, None, False),
    ("v7 without the trail", "roll_noon", False, None, False),
    *((f"SL {pct:.0%} + trail", "roll_noon", True, f"{pct}", False) for pct in PCT_GRID),
    *((f"SL {pct:.0%}, no trail", "roll_noon", False, f"{pct}", False) for pct in PCT_GRID),
) if PCT_RUN else (
    ("v7 as live: trail, no stop-loss", "roll_noon", True, None, False),
    *((f"SL {sl:,} fixed + trail", "roll_noon", True, float(sl), False) for sl in SL_GRID),
    *((f"SL {sl:,} step-up + trail", "roll_noon", True, float(sl), True) for sl in SL_GRID),
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



async def run(data: Data, days: list[date], candles, index, records, carry: str, order: str, trail: bool = True,
              stop_loss: float | str | None = None, step_up: bool = False):
    """(trades, overnight gaps, entries passed over) for one candle order."""
    trades, gaps, no_price, pos = [], [], 0, None
    reenter = trail == "reenter"

    def book(exit_price: float, reason: str, at: datetime, day: date) -> None:
        nonlocal pos
        qty, entry, option_type = pos["qty"], pos["entry"], pos["key"][2]
        cost = charges(option_type, qty, entry, True) + charges(option_type, qty, exit_price, False)
        net = (exit_price - entry) * qty - cost
        trades.append({"day": day, "opened": pos["opened"], "closed": at, "entry": entry, "exit": exit_price, "qty": qty,
                       "option": f"{pos['key'][0]:%d %b} {pos['key'][1]:,.0f} {option_type}", "reason": reason, "net": net,
                       "points": net / qty, "pct": net / (entry * qty) * 100, "nights": pos["nights"],
                       "rebought": pos.get("rebought", False), "exit_vs_paid": exit_price - entry})
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
        if pos is None or (not trail and stop_loss is None):
            return None
        bar = (await data.day_bars(pos["key"], day)).get(t)
        if bar is None:
            return None
        for i, field in enumerate(PATHS[order]):
            p = bid(bar[field])
            pos["peak"] = max(pos["peak"], p)
            if trail and pos["stop"] is None and pos["peak"] >= pos["entry"] * (1 + TRAIL_START):
                pos["stop"] = pos["peak"] * (1 - TRAIL)
            if pos["stop"] is not None:
                pos["stop"] = max(pos["stop"], pos["peak"] * (1 - TRAIL))
            sl_level = None
            if isinstance(stop_loss, str) and pos["stop"] is None:  # a % of the price paid, until the trail is on
                sl_level = pos["entry"] * (1 - float(stop_loss))
            elif stop_loss is not None and not isinstance(stop_loss, str) and (step_up or pos["stop"] is None):
                steps = max(0, int((pos["peak"] - pos["entry"]) * pos["qty"] // STEP_EVERY)) if step_up else 0
                sl_level = pos["entry"] - (stop_loss - STEP_UP * steps) / pos["qty"]
            levels = [(v, why) for v, why in ((pos["stop"], "trailing_stop"), (sl_level, "stop_loss")) if v is not None]
            if levels:
                level, why = max(levels)
                if p <= level:
                    at = datetime.combine(day, t, tzinfo=IST) + MARK_STEP / 2
                    fill = p if i == 0 else level
                    sold = {"key": pos["key"], "qty": pos["qty"], "fill": fill, "at": at if i else datetime.combine(day, t, tzinfo=IST)}
                    book(fill, f"{why}_at_open" if (i == 0 and t == time(9, 15)) else why, sold["at"], day)
                    return None
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
                           "peak": bid(bar["open"]), "stop": None, "nights": 0}
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
    return trades, gaps, no_price


def gap_summary(gaps: list[dict]) -> dict:
    pts = [g["points"] for g in gaps]
    return {"n": len(pts), "up": sum(p > 0 for p in pts), "down": sum(p < 0 for p in pts),
            "avg": statistics.mean(pts) if pts else 0.0, "sum": sum(pts), "best": max(pts, default=0.0), "worst": min(pts, default=0.0),
            "pct": statistics.mean([g["points"] / g["close"] * 100 for g in gaps if g["close"]]) if gaps else 0.0}


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
        print(f"sessions: {len(days)} ({days[0]} -> {days[-1]}); candles with a 15-min change: {len(candles)}")
        results = []
        for name, carry, trail, stop_loss, step_up in VARIANTS:
            runs = {}
            for order in PATHS:
                trades, gaps, missing = await run(data, days, candles, index, records, carry, order, trail, stop_loss, step_up)
                runs[order] = {**summary(trades, days), "trade_list": trades, "gaps": gaps, "missing": missing}
            results.append({"name": name, "carry": carry, "trail": trail, "stop_loss": stop_loss, "step_up": step_up, "runs": runs,
                            "mid": middle(*runs.values())})

    print("1 lot, net of the 0.1% half-spread and charges; points per unit summed; the middle of the two candle orders "
          "(both readings in brackets):")
    print(f"  {'variant':<40} {'points':>8} {'net% prem':>9} {'1st half':>8} {'2nd half':>8} {'win%':>5} {'trades':>6} "
          f"{'stops':>5} {'days up':>8} {'avg hold':>8}")
    for r in results:
        m, (a, b) = r["mid"], r["runs"].values()
        print(f"  {r['name']:<40} {m['points']:>+8.1f} {m['on_premium']:>+9.2f} {m['first_half_pts']:>+8.1f} {m['second_half_pts']:>+8.1f} "
              f"{m['win']:>5.1f} {m['trades']:>6.0f} {m['stops']:>5.0f} {m['days_up']:>4.0f}/{len(days):<3} {m['avg_hold']:>6.0f}m"
              f"  ({a['points']:+.1f} .. {b['points']:+.1f})")
    for r in results:
        for order, run_ in r["runs"].items():
            run_["sl_exits"] = sum(t["reason"].startswith("stop_loss") for t in run_["trade_list"])
            run_["sl_at_open"] = sum(t["reason"] == "stop_loss_at_open" for t in run_["trade_list"])
    print("stop-loss exits (high first / low first), of them at a 09:15 open after a night:")
    for r in results[1:]:
        a, b = r["runs"].values()
        print(f"  {r['name']:<32} {a['sl_exits']:>4} / {b['sl_exits']:<4}  at the open {a['sl_at_open']} / {b['sl_at_open']}")
    v7 = results[0]["mid"]["points"]
    fixed = [r for r in results[1:] if r["trail"] and r["stop_loss"] is not None]
    stepped = [r for r in results[1:] if not r["trail"]] if PCT_RUN else [r for r in results[1:] if r["step_up"]]
    for label, group in (("with the trail", fixed), ("without the trail", stepped)):
        best = max(group, key=lambda r: r["mid"]["points"])
        beats = [r["name"] for r in group if min(x["points"] for x in r["runs"].values()) > max(x["points"] for x in results[0]["runs"].values())]
        print(f"best {label}: {best['name']} {best['mid']['points']:+.1f} against v7 {v7:+.1f}; beating v7 on both readings: "
              f"{', '.join(beats) if beats else 'none'}")
    print("points per unit by day (v7, then the best fixed and the best step-up; the middle):")
    bf = max(fixed, key=lambda r: r["mid"]["points"])
    bs = max(stepped, key=lambda r: r["mid"]["points"])
    for d in days:
        print(f"  D,{d:%d %b}," + ",".join(f"{r['mid']['points_day'].get(d, 0.0):+.1f}" for r in (results[0], bf, bs)))

    lines = []
    for r in results:
        m, (a, b) = r["mid"], r["runs"].values()
        lines.append(f"{r['name']}: Rs {m['net']:+,.0f} ({a['net']:+,.0f} / {b['net']:+,.0f}); halves {m['first_half']:+,.0f} / "
                     f"{m['second_half']:+,.0f}; {m['trades']:.0f} trades, win {m['win']:.0f}%; max drawdown {m['drawdown']:,.0f}")
    await send_telegram("OI Change Buy 15 MIN: v7 with a stop-loss of Rs 500-2,000 a lot",
                        f"{len(days)} sessions ({days[0]:%d %b} - {days[-1]:%d %b}), 1 lot, stored 15-min candles, 0.1% half-spread "
                        f"and charges; the middle of the two candle orders (both in brackets).\n\n" + "\n\n".join(lines)
)
    print("sent to the private Telegram chat: rupees, yes")


if __name__ == "__main__":
    asyncio.run(main())
