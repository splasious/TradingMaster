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

The public log gets counts and percentages of the premium paid only;
rupees, per-day results and every trade go to the private Telegram chat.
Run inside the backend container (the health check's `oi_trail_backtest`
input pipes it to `python -`).
"""

import asyncio
import csv
import io
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import select

from app.core.config import get_settings
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
VARIANTS = (("no trail", None, None), ("+20% / 10%", 0.20, 0.10), ("+15% / 7.5%", 0.15, 0.075), ("+30% / 15%", 0.30, 0.15))
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
                pos = {"key": key, "entry": entry, "qty": float(found[1]), "opened": m, "peak": bid(bar["open"]), "active": False,
                       "stop": None}
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


def summary(trades: list[dict]) -> dict:
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
    }


def trades_csv(rows: list[tuple[str, str, dict]]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["variant", "candle_order", "day", "option", "opened", "closed", "reason", "qty", "entry", "exit", "peak_bid",
                "gross", "charges", "net", "net_pct_of_premium"])
    for variant, order, t in rows:
        w.writerow([variant, order, t["day"].isoformat(), t["option"], f"{t['opened']:%H:%M}", f"{t['closed']:%H:%M}", t["reason"],
                    f"{t['qty']:g}", f"{t['entry']:.2f}", f"{t['exit']:.2f}", f"{t['peak']:.2f}", f"{t['gross']:.2f}",
                    f"{t['charges']:.2f}", f"{t['net']:.2f}", f"{t['pct']:.2f}"])
    return out.getvalue().encode()


async def send_csv(data: bytes, filename: str, caption: str) -> bool:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendDocument",
            data={"chat_id": settings.telegram_chat_id, "caption": caption},
            files={"document": (filename, data, "text/csv")},
        )
    return resp.status_code == 200


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
        print(f"sessions: {len(days)} ({days[0] if days else '-'} -> {days[-1] if days else '-'}); "
              f"closes with a record: {sum(len(by_day[d]) for d in days)}; NIFTY option contracts stored: {len(data.contracts)}")

        results: dict[tuple[str, str], list[dict]] = {}
        skipped: dict[tuple[str, str], int] = {}
        for name, act, trail in VARIANTS:
            for order in (ORDERS if act is not None else ORDERS[:1]):
                all_trades, missing = [], 0
                for day in days:
                    trades, no_price = await simulate(data, day, by_day[day], act, trail, order)
                    all_trades += trades
                    missing += no_price
                results[(name, order)] = all_trades
                skipped[(name, order)] = missing

    print("results (net of the 0.1% half-spread and charges; % of the premium paid; 1 lot):")
    print(f"  {'variant':<13} {'candle order':<11} {'trades':>6} {'win%':>6} {'avg%':>7} {'median%':>8} {'best%':>7} {'worst%':>7} "
          f"{'trail exits':>11} {'days up':>8} {'net/premium%':>12}")
    lines = []
    for (name, order), trades in results.items():
        s = summary(trades)
        shown = order if name != "no trail" else "-"
        print(f"  {name:<13} {shown:<11} {s['trades']:>6} {s['win']:>6.1f} {s['avg']:>7.2f} {s['median']:>8.2f} {s['best']:>7.1f} "
              f"{s['worst']:>7.1f} {s['trail_exits']:>11} {s['days_up']:>4}/{s['days']:<3} {s['on_premium']:>12.2f}")
        lines.append(f"{name} ({shown}): {s['trades']} trades, win {s['win']:.0f}%, avg {s['avg']:+.2f}%/trade; "
                     f"net Rs {s['net']:+,.0f}, best day {s['best_day']:+,.0f}, worst day {s['worst_day']:+,.0f}, "
                     f"max drawdown {s['drawdown']:,.0f}; trail exits {s['trail_exits']}; days up {s['days_up']}/{s['days']}")
    print(f"  entries passed over for want of a candle: {max(skipped.values(), default=0)}")

    body = ("1 lot, the stored 15-min candles, 0.1% half-spread and charges on every order. A trail run both ways round "
            "(high first / low first): the truth lies between.\n\n" + "\n\n".join(lines))
    await send_telegram("OI Change Buy 15 MIN: trailing-stop backtest", body)
    rows = [(name, order, t) for (name, order), trades in results.items() for t in trades]
    sent = await send_csv(trades_csv(rows), f"oi_change_buy_trail_backtest_{datetime.now(timezone.utc):%Y%m%d}.csv",
                          "OI Change Buy 15 MIN trailing-stop backtest: every trade")
    print(f"sent to the private Telegram chat: summary yes; trades file {'yes' if sent else 'no'}")


if __name__ == "__main__":
    asyncio.run(main())
