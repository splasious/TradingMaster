"""OI Change Buy 15 MIN: trailing stops backtested on the stored data (17 Nov).

Read-only. Replays OI Change Buy 15 MIN's version 2 rules over every NIFTY
session the server has 15-minute change-in-OI records for (pcr_snapshots),
priced on the stored 15-minute NIFTY option candles (bf_ohlcv_bars), with
and without a trailing stop -- agreed 17 Nov, "only the 37 sessions":

- At each close 09:30-15:15, that record's change in OI over the last 15
  minutes: put side higher, the ATM call; call side higher, the ATM put;
  equal (or no record), no change. ATM: the 50-point strike nearest the
  record's NIFTY. The nearest weekly in the record's expiries, next week's
  on its expiry day.
- Bought at the candle's open + HALF_SPREAD (about the ask), sold at a
  price - HALF_SPREAD (about the bid); 1 lot; the card's charges
  (trade_record.order_charges) on every order.
- Exits: a flip at a close (the held option sold at that candle's open, the
  other bought at its own); 15:29 (the 15:15 candle's close); and the
  trailing stop under test -- active once the bid is ACTIVATE above the
  price paid, then TRAIL below the highest bid since, only moving up. After
  a trail exit: flat until the next close, then that close's side.
- A 15-minute candle has its high and low but not which came first, so
  each trail is run twice: the candle walked open -> high -> low -> close
  ("high first") and open -> low -> high -> close ("low first"); the truth
  lies between, mostly. A candle opening under the stop fills at its open.

A grid (17 Nov, "what best trail SL can be placed to maximise the
profit"): every ACTIVATE x TRAIL below -- ACTIVATE 0 being a trail from the
entry itself, so also a stop-loss -- ranked by the rupee P&L of 1 lot,
taken as the middle of its two candle orders, with each half of the
sessions shown apart (a setting picked on 36 days that only works in one
half is luck). The public log gets the ranking, counts and percentages of
the premium paid only; the rupee results go to the private Telegram chat.
Run inside the backend container (the health check's `oi_trail_backtest`
input pipes it to `python -`).
"""

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
MARK_STEP = timedelta(minutes=15)
STRIKE_STEP = 50
HALF_SPREAD = 0.001  # 0.1% of the price each way
DEFAULT_LOT = 65
ACTIVATE = (0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.75, 1.00)
TRAIL = (0.05, 0.075, 0.10, 0.125, 0.15, 0.20, 0.25, 0.30, 0.40)
ORDERS = ("high first", "low first")
PATHS = {"high first": ("open", "high", "low", "close"), "low first": ("open", "low", "high", "close")}


def marks_of(day: date) -> list[datetime]:
    first, last = datetime.combine(day, FIRST_MARK, tzinfo=IST), datetime.combine(day, LAST_MARK, tzinfo=IST)
    out, m = [], first
    while m <= last:
        out.append(m)
        m += MARK_STEP
    return out


def atm(spot: float) -> float:
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def wanted(put: float | None, call: float | None) -> str | None:
    if put is None or call is None or put == call:
        return None
    return "CE" if put > call else "PE"


def bid(price: float) -> float:
    return price * (1 - HALF_SPREAD)


def ask(price: float) -> float:
    return price * (1 + HALF_SPREAD)


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

    async def day_bars(self, key: tuple[date, float, str], day: date) -> dict[time, dict]:
        """The contract's 15-minute candles that day, by start time (IST)."""
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
                    bars[as_aware_utc(ts).astimezone(IST).time()] = {"open": float(o), "high": float(h), "low": float(lo), "close": float(c)}
        self.bars[(key, day)] = bars
        return bars


def charges(option_type: str, qty: float, price: float, buying: bool) -> float:
    return order_charges({"option_type": option_type, "quantity": qty}, price, buying, False) or 0.0


async def simulate(data: Data, day: date, records: dict[time, PcrSnapshot], act, trail, order) -> tuple[list[dict], int]:
    """(the day's trades, entries passed over for want of a candle)."""
    trades, no_price, pos = [], 0, None

    def book(exit_price: float, reason: str, at: datetime) -> None:
        nonlocal pos
        qty, entry, option_type = pos["qty"], pos["entry"], pos["key"][2]
        gross = (exit_price - entry) * qty
        cost = charges(option_type, qty, entry, True) + charges(option_type, qty, exit_price, False)
        net = gross - cost
        trades.append({"day": day, "option": f"{pos['key'][0]:%d %b} {pos['key'][1]:,.0f} {option_type}", "opened": pos["opened"],
                       "closed": at, "entry": entry, "exit": exit_price, "peak": pos["peak"], "qty": qty, "reason": reason,
                       "gross": gross, "charges": cost, "net": net, "pct": net / (entry * qty) * 100})
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
        side = wanted(rec.put_oi_change, rec.call_oi_change) if rec is not None else None
        if pos is not None and side is not None and side != pos["key"][2]:
            price = await price_at(pos["key"], m)
            if price is not None:
                book(bid(price), "oi_flip", m)
        if pos is None and side is not None and rec.spot:
            expiries = [date.fromisoformat(e) for e in (rec.expiries or [])]
            expiries = [e for e in expiries if e >= day]
            weekly = (expiries[1] if expiries[0] == day and len(expiries) > 1 else expiries[0]) if expiries else None
            key = (weekly, atm(rec.spot), side)
            bar = (await data.day_bars(key, day)).get(m.time()) if weekly is not None else None
            if bar is None:
                no_price += 1
            else:
                entry = ask(bar["open"])
                found = data.contracts[key]
                pos = {"key": key, "entry": entry, "qty": float(found[1]), "opened": m, "peak": bid(bar["open"]),
                       "active": act == 0, "stop": bid(bar["open"]) * (1 - trail) if act == 0 else None}
        if pos is not None and act is not None:
            bar = (await data.day_bars(pos["key"], day)).get(m.time())
            if bar is not None:
                for i, field in enumerate(PATHS[order]):
                    p = bid(bar[field])
                    if pos["active"] and p <= pos["stop"]:
                        book(p if i == 0 else pos["stop"], "trailing_stop", m + MARK_STEP / 2)
                        break
                    pos["peak"] = max(pos["peak"], p)
                    if not pos["active"] and pos["peak"] >= pos["entry"] * (1 + act):
                        pos["active"] = True
                    if pos["active"]:
                        pos["stop"] = max(pos["stop"] or 0.0, pos["peak"] * (1 - trail))
        elif pos is not None:
            bar = (await data.day_bars(pos["key"], day)).get(m.time())
            if bar is not None:
                pos["peak"] = max(pos["peak"], bid(bar["high"]))
    if pos is not None:
        price = await price_at(pos["key"], datetime.combine(day, LAST_MARK, tzinfo=IST), "close")
        book(bid(price if price is not None else pos["entry"]), "time_cutoff_329pm", datetime.combine(day, time(15, 29), tzinfo=IST))
    return trades, no_price


def summary(trades: list[dict], halves: tuple[set, set] = (set(), set())) -> dict:
    pcts = [t["pct"] for t in trades]
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["day"]] += t["net"]
    equity = peak = drawdown = 0.0
    for day in sorted(by_day):
        equity += by_day[day]
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    cost = sum(t["entry"] * t["qty"] for t in trades)
    return {
        "trades": len(trades), "win": sum(p > 0 for p in pcts) / len(pcts) * 100 if pcts else 0.0,
        "avg": statistics.mean(pcts) if pcts else 0.0, "median": statistics.median(pcts) if pcts else 0.0,
        "best": max(pcts, default=0.0), "worst": min(pcts, default=0.0),
        "trail_exits": sum(t["reason"] == "trailing_stop" for t in trades),
        "days_up": sum(v > 0 for v in by_day.values()), "days": len(by_day),
        "net": sum(t["net"] for t in trades), "on_premium": sum(t["net"] for t in trades) / cost * 100 if cost else 0.0,
        "drawdown": drawdown, "best_day": max(by_day.values(), default=0.0), "worst_day": min(by_day.values(), default=0.0),
        "first_half": sum(v for d, v in by_day.items() if d in halves[0]),
        "second_half": sum(v for d, v in by_day.items() if d in halves[1]),
        "premium": cost,
    }


def label(g: dict) -> str:
    return f"+{g['activate'] * 100:g}% / {g['trail'] * 100:g}%" if g["activate"] else f"from entry / {g['trail'] * 100:g}%"


def pct(value: float, premium: float) -> float:
    return value / premium * 100 if premium else 0.0


async def main() -> None:
    async with AsyncSessionLocal() as db:
        data = Data(db)
        await data.load_contracts()
        snaps = (await db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == "NIFTY").order_by(PcrSnapshot.ts))).scalars().all()
        by_day: dict[date, dict[time, PcrSnapshot]] = defaultdict(dict)
        for snap in snaps:
            at = as_aware_utc(snap.ts).astimezone(IST)
            if FIRST_MARK <= at.time() <= LAST_MARK:
                by_day[at.date()][at.time()] = snap
        today = datetime.now(timezone.utc).astimezone(IST).date()
        days = [d for d in sorted(by_day) if d < today]  # whole sessions only
        halves = (set(days[: len(days) // 2]), set(days[len(days) // 2:]))
        print(f"sessions: {len(days)} ({days[0] if days else '-'} -> {days[-1] if days else '-'}; halves of "
              f"{len(halves[0])} and {len(halves[1])}); closes with a record: {sum(len(by_day[d]) for d in days)}; "
              f"NIFTY option contracts stored: {len(data.contracts)}")

        async def run(act, trail, order) -> tuple[list[dict], int]:
            all_trades, missing = [], 0
            for day in days:
                trades, no_price = await simulate(data, day, by_day[day], act, trail, order)
                all_trades += trades
                missing += no_price
            return all_trades, missing

        base_trades, missing = await run(None, None, ORDERS[0])
        base = summary(base_trades, halves)
        grid = []
        for act in ACTIVATE:
            for trail in TRAIL:
                runs = {order: summary((await run(act, trail, order))[0], halves) for order in ORDERS}
                mid = {k: (runs[ORDERS[0]][k] + runs[ORDERS[1]][k]) / 2
                       for k in ("net", "premium", "first_half", "second_half", "win", "avg", "drawdown")}
                grid.append({"activate": act, "trail": trail, "mid": mid, "runs": runs})

    grid.sort(key=lambda g: g["mid"]["net"], reverse=True)
    # Public: the ranking (by 1 lot's rupee net, the middle of the two candle orders) in % of the premium paid only.
    print("ranked by 1 lot's net P&L, the middle of high-first and low-first; shown as % of the premium paid, net of the 0.1% "
          "half-spread and charges:")
    print(f"  {'rank':>4} {'setting':<20} {'net% mid':>8} {'(high..low)':<17} {'1st half%':>9} {'2nd half%':>9} {'win%':>6} "
          f"{'trades':>9} {'trail exits':>11}")
    print(f"  {'-':>4} {'no trail (today)':<20} {pct(base['net'], base['premium']):>+8.2f} {'':<17} "
          f"{pct(base['first_half'], base['premium']):>+9.2f} {pct(base['second_half'], base['premium']):>+9.2f} {base['win']:>6.1f} "
          f"{base['trades']:>9} {0:>11}")
    for rank, g in enumerate(grid[:20], 1):
        a, b = g["runs"][ORDERS[0]], g["runs"][ORDERS[1]]
        span = f"({pct(a['net'], a['premium']):+.2f}..{pct(b['net'], b['premium']):+.2f})"
        print(f"  {rank:>4} {label(g):<20} {pct(g['mid']['net'], g['mid']['premium']):>+8.2f} {span:<17} "
              f"{pct(g['mid']['first_half'], g['mid']['premium']):>+9.2f} {pct(g['mid']['second_half'], g['mid']['premium']):>+9.2f} "
              f"{g['mid']['win']:>6.1f} {a['trades']:>4}-{b['trades']:<4} {a['trail_exits']:>5}-{b['trail_exits']:<5}")
    beat_mid = sum(g["mid"]["net"] > base["net"] for g in grid)
    beat_both = sum(min(r["net"] for r in g["runs"].values()) > base["net"] for g in grid)
    print(f"  settings tried: {len(grid)}; beating no trail on the middle reading: {beat_mid}; on both readings: {beat_both}")
    print(f"  entries passed over for want of a candle: {missing}")

    # Private: rupees.
    lines = [(f"No trail (today): net Rs {base['net']:+,.0f}; {base['trades']} trades, win {base['win']:.0f}%; "
              f"1st half {base['first_half']:+,.0f}, 2nd half {base['second_half']:+,.0f}; best day {base['best_day']:+,.0f}, "
              f"worst day {base['worst_day']:+,.0f}; max drawdown {base['drawdown']:,.0f}")]
    for rank, g in enumerate(grid[:10], 1):
        a, b = g["runs"][ORDERS[0]], g["runs"][ORDERS[1]]
        lines.append(f"#{rank} {label(g)}: net Rs {g['mid']['net']:+,.0f} (high first {a['net']:+,.0f}, low first {b['net']:+,.0f}); "
                     f"1st half {g['mid']['first_half']:+,.0f}, 2nd half {g['mid']['second_half']:+,.0f}; "
                     f"max drawdown {a['drawdown']:,.0f} / {b['drawdown']:,.0f}")
    await send_telegram("OI Change Buy 15 MIN: trailing-stop grid backtest",
                        f"{len(days)} sessions ({days[0]:%d %b} - {days[-1]:%d %b}), 1 lot, stored 15-min candles, 0.1% half-spread and "
                        f"charges. No trail, then the best 10 of {len(grid)} settings by net P&L (the middle of the two candle "
                        f"orders):\n\n" + "\n\n".join(lines))
    print("sent to the private Telegram chat: rupee summary yes")


if __name__ == "__main__":
    asyncio.run(main())
