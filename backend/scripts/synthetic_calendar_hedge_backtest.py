"""Synthetic Calendar 15 MIN and its Reverse: the hedge a weekly instead of the monthly, backtested (26 Nov).

Read-only. Asked 26 Nov: "Synthetic Calendar 15 MIN and Synthetic Calendar
Reverse 15 MIN ... shift current week and hedge with gap of one week ...
check backtest results". Today's rules replayed over every NIFTY session
with 15-minute records (pcr_snapshots) before today, for each strategy with
three hedges -- the monthly (today's), the next weekly (a week after the
current one), and the weekly two weeks out (one week skipped):

  entry    from 09:30 to 15:00 whenever none is open (after a close, at that
           record's prices: the card waits a minute): all four legs at the
           50-point strike nearest NIFTY, 20 lots a leg, a buy at the ask
           and a sell at the bid -- Synthetic Calendar: the current weekly's
           CE bought and PE sold, the hedge's PE bought and CE sold;
           Reverse: every leg the other way. The current weekly: the
           nearest expiry, next week's on an expiry day.
  hedge    monthly: the nearest monthly after the weekly (today's rule);
           next: the listed expiry 4-10 days after the weekly; two weeks:
           11-17 days after it (nearest 7 and 14: an expiry a holiday
           brings forward still counts)
  exits    take-profit: Rs 14,000 after charges at bid/ask; else all four
           closed at 15:00 on the weekly's expiry day (the first record
           from 15:00), in profit or not; nothing new that day

How it is priced: each 15-minute record's per-contract last price
(pcr_strike_oi; the stored 15-minute candle's close where a record has
none), a sell at the price less HALF_SPREAD, a buy at it plus; the card's
charges on all eight orders. Checked only at each record (the card checks
every second): a target seen after an earlier check that day is booked at
the target itself, the first check of a day at what it is. A synthetic's
legs are priced at their own last trades, so the backtest's "net if closed
now" carries some noise the live order book doesn't.

The public log gets the rules, counts and results in points per unit (net
rupees / the 1,300 units a leg) -- no rupees. Run inside the backend
container (the health check's `synthetic_hedge_backtest` input pipes it to
`python -`).
"""

import asyncio
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.paper_trading import PaperNativeDeployment, PaperNativeTrade
from app.models.pcr import PcrSnapshot, PcrStrikeOi
from app.models.strategy import Strategy
from app.services.broker.zerodha_broker import IST
from app.services.paper_trading.trade_record import order_charges

UNDERLYING = "NIFTY"
HALF_SPREAD = 0.001  # 0.1% of the price each way, as the other calendar backtests
STRIKE_PAD = 400.0  # points either side of the session's NIFTY range loaded up front
LOOKAHEAD = timedelta(days=50)  # expiries loaded each session: the weekly and a monthly after it
FIRST_RECORD, CLOSE_RECORD = dtime(9, 15), dtime(15, 30)
ENTRY_TIME, ENTRY_UNTIL, WEEKLY_CLOSE = dtime(9, 30), dtime(15, 0), dtime(15, 0)
STRIKE_STEP = 50
LOTS_PER_LEG, DEFAULT_LOT = 20, 65
TAKE_PROFIT = 14_000.0
HEDGES = {"monthly": None, "next weekly": (4, 10, 7), "two weeks out": (11, 17, 14)}
LEGS = {  # name -> (option type, side in Synthetic Calendar, which expiry); Reverse: every side the other way
    "weekly_ce": ("CE", "buy", "weekly"), "weekly_pe": ("PE", "sell", "weekly"),
    "hedge_pe": ("PE", "buy", "hedge"), "hedge_ce": ("CE", "sell", "hedge"),
}


def ist(dt: datetime) -> datetime:
    return as_aware_utc(dt).astimezone(IST)


def bid(p: float) -> float:
    return p * (1 - HALF_SPREAD)


def ask(p: float) -> float:
    return p * (1 + HALF_SPREAD)


def charges(option_type: str, quantity: float, price: float, buying: bool) -> float:
    return order_charges({"option_type": option_type, "quantity": quantity}, price, buying, False) or 0.0


def failed(where: str, exc: BaseException) -> None:
    """The kind and this script's line only -- never the message text."""
    lines = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename in ("<stdin>", __file__)]
    print(f"  {where}: not run ({type(exc).__name__} at line {lines[-1] if lines else '?'})")


class Market:
    """The stored records and prices, loaded once and shared by both runs."""

    def __init__(self, db):
        self.db = db
        self.by_day: dict[date, list[tuple[dtime, PcrSnapshot]]] = {}
        self.expiries: list[date] = []
        self.month_last: set[date] = set()
        self.px: dict[tuple, float | None] = {}  # (record id, expiry, strike, type) -> last price
        self.loaded: set[tuple] = set()
        self.symbols: dict[tuple, object] = {}
        self.bars: dict[tuple, dict[dtime, float]] = {}
        self.source = Counter()
        self.lot = DEFAULT_LOT

    async def load(self, before: date) -> None:
        snaps = (await self.db.execute(
            select(PcrSnapshot).where(PcrSnapshot.underlying == UNDERLYING, PcrSnapshot.session_date < before).order_by(PcrSnapshot.ts)
        )).scalars().all()
        known: set[date] = set()
        for s in snaps:
            t = ist(s.ts).time()
            if FIRST_RECORD <= t <= CLOSE_RECORD and s.spot:
                self.by_day.setdefault(s.session_date, []).append((t, s))
            for e in s.expiries or []:
                known.add(date.fromisoformat(str(e)))
        rows = (await self.db.execute(
            select(BfSymbol.id, BfSymbol.expiry, BfSymbol.strike, BfSymbol.option_type, BfSymbol.lot_size)
            .where(BfSymbol.underlying_symbol == UNDERLYING, BfSymbol.option_type.is_not(None))
        )).all()
        lots = Counter()
        for sid, e, k, t, lot in rows:
            if e and k is not None:
                self.symbols[(e, float(k), t)] = sid
                known.add(e)
                if lot:
                    lots[lot] += 1
        if lots:
            self.lot = lots.most_common(1)[0][0]
        self.expiries = sorted(known)
        last: dict[tuple[int, int], date] = {}
        for e in self.expiries:
            last[(e.year, e.month)] = max(e, last.get((e.year, e.month), e))
        self.month_last = set(last.values())

    def monthly_after(self, weekly: date) -> date | None:
        return next((e for e in self.expiries if e > weekly and e in self.month_last), None)

    @property
    def sessions(self) -> list[date]:
        return sorted(d for d, recs in self.by_day.items() if any(t >= ENTRY_TIME for t, _ in recs))

    def listed(self, d: date) -> list[date]:
        return [e for e in self.expiries if e >= d][:8]

    async def load_day(self, d: date) -> None:
        recs = [s for _, s in self.by_day.get(d, [])]
        need = [e for e in self.expiries if d <= e <= d + LOOKAHEAD]
        spots = [s.spot for s in recs if s.spot]
        if not spots or not need:
            return
        lo, hi = min(spots) - STRIKE_PAD, max(spots) + STRIKE_PAD
        rows = (await self.db.execute(
            select(PcrStrikeOi.snapshot_id, PcrStrikeOi.expiry, PcrStrikeOi.strike, PcrStrikeOi.option_type, PcrStrikeOi.last_price)
            .where(PcrStrikeOi.snapshot_id.in_([s.id for s in recs]), PcrStrikeOi.expiry.in_(need), PcrStrikeOi.strike.between(lo, hi))
        )).all()
        for sid, e, k, t, lp in rows:
            self.px[(sid, e, float(k), t)] = lp
        for e in need:
            self.loaded.add((d, e, "range", lo, hi))

    def _in_range(self, d: date, e: date, k: float) -> bool:
        return any(key[0] == d and key[1] == e and key[3] <= k <= key[4] for key in self.loaded if key[2] == "range")

    async def _lazy(self, d: date, e: date, k: float) -> None:
        if (d, e, k) in self.loaded or self._in_range(d, e, k):
            return
        self.loaded.add((d, e, k))
        ids = [s.id for _, s in self.by_day.get(d, [])]
        rows = (await self.db.execute(
            select(PcrStrikeOi.snapshot_id, PcrStrikeOi.option_type, PcrStrikeOi.last_price)
            .where(PcrStrikeOi.snapshot_id.in_(ids), PcrStrikeOi.expiry == e, PcrStrikeOi.strike == k)
        )).all()
        for sid, t, lp in rows:
            self.px[(sid, e, k, t)] = lp

    async def price(self, d: date, t: dtime, rec: PcrSnapshot, e: date, k: float, typ: str) -> float | None:
        key = (rec.id, e, float(k), typ)
        if key not in self.px:
            await self._lazy(d, e, float(k))
        found = self.px.get(key)
        if found:
            self.source["record"] += 1
            return float(found)
        sid = self.symbols.get((e, float(k), typ))
        if sid is not None:
            if (sid, d) not in self.bars:
                start = datetime.combine(d, dtime(9, 0), IST).astimezone(timezone.utc)
                bars = (await self.db.execute(
                    select(BfOhlcvBar.ts, BfOhlcvBar.close).where(
                        BfOhlcvBar.symbol_id == sid, BfOhlcvBar.timeframe == "15m",
                        BfOhlcvBar.ts >= start, BfOhlcvBar.ts < start + timedelta(hours=7))
                )).all()
                self.bars[(sid, d)] = {ist(ts).time(): float(c) for ts, c in bars if c}
            close = self.bars[(sid, d)].get((datetime.combine(d, t) - timedelta(minutes=15)).time())
            if close:
                self.source["candle"] += 1
                return close
        self.source["none"] += 1
        return None


def atm_strike(spot: float) -> float:
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def closing(leg: dict, mid: float) -> float:
    return ask(mid) if leg["side"] == "sell" else bid(mid)


def leg_net(leg: dict, mid: float) -> float:
    """Closing `leg` at `mid`'s bid/ask: its P&L less its opening and closing charges."""
    price = closing(leg, mid)
    diff = leg["entry"] - price if leg["side"] == "sell" else price - leg["entry"]
    return diff * leg["qty"] - leg["entry_charges"] - charges(leg["type"], leg["qty"], price, buying=leg["side"] == "sell")


class Run:
    def __init__(self, m: Market, reverse: bool, hedge: str):
        self.m, self.reverse, self.hedge = m, reverse, hedge
        self.name = f"{'Synthetic Calendar Reverse' if reverse else 'Synthetic Calendar'}, hedge {hedge}"
        self.qty = float(LOTS_PER_LEG * m.lot)
        self.position: dict | None = None
        self.trades: list[dict] = []
        self.waits = Counter()
        self.open_left: float | None = None

    def hedge_for(self, weekly: date) -> date | None:
        rule = HEDGES[self.hedge]
        if rule is None:
            return self.m.monthly_after(weekly)
        lo, hi, aim = rule
        near = [e for e in self.m.expiries if lo <= (e - weekly).days <= hi]
        return min(near, key=lambda e: abs((e - weekly).days - aim)) if near else None

    def net(self) -> float:
        return sum(leg_net(leg, leg["last"]) for leg in self.position["legs"].values())

    def close(self, reason: str, d: date, t: dtime, at_target: bool = False) -> None:
        net = TAKE_PROFIT if at_target else self.net()
        p = self.position
        self.trades.append({"kind": reason, "net": net, "opened": p["opened"], "closed": datetime.combine(d, t, IST),
                            "gap_tp": reason == "take_profit" and not at_target})
        self.position = None

    async def open_position(self, d: date, t: dtime, rec) -> bool:
        listed = self.m.listed(d)
        weekly = (listed[1] if len(listed) > 1 else None) if listed and listed[0] == d else (listed[0] if listed else None)
        hedge = self.hedge_for(weekly) if weekly else None
        if hedge is None:
            self.waits["no hedge expiry listed"] += 1
            return False
        strike = atm_strike(rec.spot)
        legs = {}
        for name, (typ, side, which) in LEGS.items():
            if self.reverse:
                side = "sell" if side == "buy" else "buy"
            expiry = weekly if which == "weekly" else hedge
            p = await self.m.price(d, t, rec, expiry, strike, typ)
            if p is None:
                self.waits["a leg unpriced"] += 1
                return False
            price = bid(p) if side == "sell" else ask(p)
            legs[name] = {"type": typ, "side": side, "expiry": expiry, "strike": strike, "qty": self.qty, "entry": price, "last": p,
                          "entry_charges": charges(typ, self.qty, price, buying=side == "buy")}
        self.position = {"legs": legs, "weekly": weekly, "hedge": hedge, "opened": datetime.combine(d, t, IST), "checked_on": None}
        return True

    async def go(self) -> None:
        for d in self.m.sessions:
            await self.m.load_day(d)
            closed_today = False
            for t, rec in self.m.by_day[d]:
                if self.position is None and t < ENTRY_TIME:
                    continue
                if self.position is not None:
                    seen = 0
                    for leg in self.position["legs"].values():
                        p = await self.m.price(d, t, rec, leg["expiry"], leg["strike"], leg["type"])
                        if p:
                            leg["last"], seen = p, seen + 1
                    if seen == 4 and self.net() >= TAKE_PROFIT:
                        self.close("take_profit", d, t, at_target=self.position["checked_on"] == d)
                    elif seen == 4:
                        self.position["checked_on"] = d
                if self.position is not None:
                    weekly = self.position["weekly"]
                    if weekly < d or (weekly == d and t >= WEEKLY_CLOSE):
                        self.close("weekly_expiry_3pm", d, t)
                        closed_today = True
                if self.position is None and not closed_today and ENTRY_TIME <= t < ENTRY_UNTIL:
                    await self.open_position(d, t, rec)
        if self.position is not None:
            self.open_left = self.net()


def report(r: Run, sessions: list[date], units: float) -> None:
    mid = sessions[len(sessions) // 2]
    nets = [x["net"] for x in r.trades]
    total = sum(nets) + (r.open_left or 0.0)
    first = sum(x["net"] for x in r.trades if x["closed"].date() < mid)
    peak = run_ = dd = 0.0
    for x in nets:
        run_ += x
        peak, dd = max(peak, run_), max(dd, peak - run_)
    kinds = Counter(x["kind"] for x in r.trades)
    held = sorted((x["closed"] - x["opened"]).total_seconds() / 3600 for x in r.trades)
    expiry = [x["net"] for x in r.trades if x["kind"] == "weekly_expiry_3pm"]
    print(f"{r.name}")
    print(f"  net {total / units:+.1f} pts a unit (1st half {first / units:+.1f}, 2nd {(total - first) / units:+.1f}); "
          f"max drawdown {dd / units:.1f}" + (f"; still open {r.open_left / units:+.1f}" if r.open_left is not None else ""))
    if nets:
        print(f"  {len(nets)} trades, {sum(1 for x in nets if x > 0)} in profit; {kinds['take_profit']} at the target "
              f"({sum(1 for x in r.trades if x['gap_tp'])} on a day's first check, past it), {kinds['weekly_expiry_3pm']} closed at the "
              f"weekly's 15:00 ({sum(expiry) / units:+.1f} pts in all, worst {min(expiry, default=0.0) / units:+.1f}); "
              f"average {sum(nets) / len(nets) / units:+.1f}, worst {min(nets) / units:+.1f}; median hold {held[len(held) // 2]:.1f} h")
    if r.waits:
        print("  no entry: " + "; ".join(f"{k} {v}" for k, v in sorted(r.waits.items())))


async def parity_noise(m: Market, hedge: str) -> None:
    """How much the gap between the hedge's and the weekly's synthetic (CE - PE at the strike nearest NIFTY)
    moves from one record to the next. In theory it moves with NIFTY times the carry between the two expiries
    (about 0.002-0.005 a point of NIFTY): a few hundredths of a point a record."""
    r = Run(m, False, hedge)
    moves, opens, theory = [], [], []
    last = None  # carried overnight: the 09:15 record against the day before's last
    for d in m.sessions:
        await m.load_day(d)
        listed = m.listed(d)
        weekly = (listed[1] if len(listed) > 1 else None) if listed and listed[0] == d else (listed[0] if listed else None)
        h = r.hedge_for(weekly) if weekly else None
        if h is None:
            last = None
            continue
        for t, rec in m.by_day[d]:
            k = atm_strike(rec.spot)
            px = [await m.price(d, t, rec, e, k, typ) for e in (weekly, h) for typ in ("CE", "PE")]
            if None in px:
                last = None
                continue
            gap = (px[2] - px[3]) - (px[0] - px[1])  # the hedge's synthetic over the weekly's, same strike
            if last is not None and last[0] == (weekly, h, k):
                (opens if t == FIRST_RECORD else moves).append(abs(gap - last[1]))
                theory.append(abs(rec.spot - last[2]) * 0.065 * (h - weekly).days / 365)
            last = ((weekly, h, k), gap, rec.spot)
    for name, xs in (("record to record", moves), ("into 09:15", opens), ("from NIFTY's move (theory)", theory)):
        xs = sorted(xs)
        if xs:
            print(f"  {hedge:<14} {name:<28} median {xs[len(xs) // 2]:6.2f} pts, 90% under {xs[int(len(xs) * 0.9)]:6.2f}, "
                  f"largest {xs[-1]:7.2f} ({len(xs)} pairs)")


async def live_cards(db, units_default: float) -> None:
    """The two strategies' paper trades so far (live bid/ask): counts, exit reasons and points per unit."""
    for name in ("Synthetic Calendar 15 MIN", "Synthetic Calendar Reverse 15 MIN"):
        rows = (await db.execute(
            select(PaperNativeTrade.exit_reason, PaperNativeTrade.pnl, PaperNativeTrade.charges, PaperNativeTrade.legs,
                   PaperNativeTrade.opened_at, PaperNativeTrade.closed_at)
            .join(PaperNativeDeployment, PaperNativeDeployment.id == PaperNativeTrade.deployment_id)
            .join(Strategy, Strategy.id == PaperNativeDeployment.strategy_id)
            .where(Strategy.name == name).order_by(PaperNativeTrade.closed_at)
        )).all()
        if not rows:
            print(f"  {name}: no paper trades yet")
            continue
        nets, reasons = [], Counter()
        for reason, pnl, chg, legs, opened, closed in rows:
            units = float((legs or [{}])[0].get("quantity") or units_default)
            nets.append((pnl - (chg or 0.0)) / units)
            reasons[reason] += 1
            print(f"    {name[:-7]}: {ist(opened):%d %b %H:%M} -> {ist(closed):%d %b %H:%M} {reason:<18} {nets[-1]:+7.1f} pts a unit")
        print(f"  {name}: {len(rows)} trades ({', '.join(f'{k} {v}' for k, v in reasons.items())}), net {sum(nets):+.1f} pts a unit")


async def main() -> None:
    today = datetime.now(timezone.utc).astimezone(IST).date()
    print("Rules (points per unit after charges and the spread; no rupees here):")
    print("  09:30-15:00 when none is open: four legs at the 50-pt strike nearest NIFTY, 20 lots, buy at the ask, sell at the bid --")
    print("    Synthetic Calendar: current weekly CE bought / PE sold, the hedge's PE bought / CE sold; Reverse: every leg the other way")
    print("  current weekly: the nearest expiry (next week's on an expiry day); hedge: the monthly after it (today's), the next")
    print("    weekly (4-10 days after it), or the weekly two weeks out (11-17 days)")
    print("  exits: Rs 14,000 after charges at bid/ask; else all four at 15:00 on the weekly's expiry day; nothing new that day")
    print(f"  priced at each 15-min record's last price (the candle's close where none) +-{100 * HALF_SPREAD:.1f}%; checked at records only")
    async with AsyncSessionLocal() as db:
        m = Market(db)
        await m.load(today)
        sessions = m.sessions
        if not sessions:
            print("no sessions with records")
            return
        units = float(LOTS_PER_LEG * m.lot)
        print(f"sessions: {len(sessions)} ({sessions[0]:%d %b} - {sessions[-1]:%d %b}; 2nd half from {sessions[len(sessions) // 2]:%d %b}); "
              f"records: {sum(len(v) for v in m.by_day.values())}; expiries known: {len(m.expiries)}; lot {m.lot}")
        print()
        for reverse in (False, True):
            for hedge in HEDGES:
                r = Run(m, reverse, hedge)
                try:
                    await r.go()
                    report(r, sessions, units)
                except Exception as exc:  # noqa: BLE001
                    failed(r.name, exc)
                print()
        print(f"prices read: {m.source['record']} from records, {m.source['candle']} from candles, {m.source['none']} not found")
        print()
        print("Check 1 -- the price noise: the gap between the two synthetics, record to record (points):")
        for hedge in HEDGES:
            try:
                await parity_noise(m, hedge)
            except Exception as exc:  # noqa: BLE001
                failed(f"parity noise {hedge}", exc)
        print()
        print("Check 2 -- the paper cards' own trades (live bid/ask, checked every second), net of charges:")
        try:
            await live_cards(db, units)
        except Exception as exc:  # noqa: BLE001
            failed("paper trades", exc)


if __name__ == "__main__":
    asyncio.run(main())
