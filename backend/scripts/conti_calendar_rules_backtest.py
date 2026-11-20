"""Conti Calendar 15 MIN: four rules backtested on the stored data (20 Nov).

Read-only. Agreed 20 Nov after the 6 Nov review: today's rules (version
13) replayed over every NIFTY session with 15-minute change-in-OI records
(pcr_snapshots) before today, priced at each record's per-contract last
price (pcr_strike_oi; the stored 15-minute candle's close where a record has
none), then the same with each of four rules -- one at a time, then the best
of each family two by two, then all four:

  baseline   version 13: from 09:30, the weekly CE+PE sold and the monthly
             after it bought at the strike with the most weekly CE+PE OI
             added since the previous close (of the 50-point strike nearest
             NIFTY and 3 either side; the PCR's premium rule without a
             record); 20 lots a leg; take-profit Rs 14,000 after charges at
             bid/ask; the next one after a close (none after a close from
             14:00); new ones until 15:00; at 15:29 closed in profit, carried
             in loss; on the weekly's expiry day at 15:00 closed -- in loss
             rolled into a new one at once, in profit nothing new that day.
             Also run with the PCR's rule picking the strike (version 12).
  drift      closed when NIFTY is DRIFT points or more from the strike, and
             the next opened by the usual rules
  cut-off    no new position from ENTRY_UNTIL (in place of 15:00)
  ratio      opened only when the monthly / weekly straddle ratio, divided by
             sqrt(monthly days left / weekly days left), is at or below
             RATIO -- days in calendar days or in trading sessions
  weekly     opened only when the weekly has 2 trading sessions or less
             left (or only when more)

How it is priced: a sell at the price less HALF_SPREAD, a buy at the price
plus it (the bid / ask the card trades at); the card's charges on all eight
orders (the strategy's own if_closed_now / charges_of). Checked only at each
15-minute record (the card checks every second): a take-profit seen at a
record is booked at the target itself when the position was checked earlier
that day (the card closes as it gets there), at what it is on the first
check of a day (an overnight gap; a carried position is checked at 09:15
too); re-entry is at that record's prices (the card re-enters a minute
later). The drift guard is measured from the strike, or from NIFTY at entry
(the strike can start up to 150 points from NIFTY). The 15:30 record stands
for 15:29.

The public log gets the rules, counts, win rates, and results in points per
unit (net rupees / the 1,300 units a leg) -- each half of the sessions
apart, a rule that only works in one half being luck. Rupees at 20 lots go
to the private Telegram chat only. Run inside the backend container (the
health check's `cc_rule_backtest` input pipes it to `python -`).
"""

import asyncio
import itertools
import math
import traceback
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.pcr import PcrSnapshot, PcrStrikeOi
from app.services.backfill_platform.coverage import is_trading_day
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram
from app.services.strategy.native_strategies import nifty_conti_calendar as cal

UNDERLYING = "NIFTY"
HALF_SPREAD = 0.001  # 0.1% of the price each way, as the OI Change Buy backtest
STRIKE_PAD = 800.0  # points either side of the session's NIFTY range loaded up front
FIRST_RECORD, CLOSE_RECORD = dtime(9, 15), dtime(15, 30)  # the 15:30 record stands for 15:29
EXPIRY_CLOSE = dtime(15, 30)


@dataclass(frozen=True)
class Rule:
    name: str
    strike_rule: str = "oi"  # "oi" (version 13) or "pcr" (version 12)
    drift: float | None = None
    drift_from: str = "strike"  # "strike", or "entry" (NIFTY where it was at entry)
    entry_until: dtime = cal.ENTRY_UNTIL
    ratio: tuple[str, float] | None = None  # ("calendar" | "trading", the most allowed)
    weekly: tuple[str, float] | None = None  # ("le" | "gt", trading sessions)
    family: str = "baseline"


def ist(dt: datetime) -> datetime:
    return as_aware_utc(dt).astimezone(IST)


def bid(p: float) -> float:
    return p * (1 - HALF_SPREAD)


def ask(p: float) -> float:
    return p * (1 + HALF_SPREAD)


def failed(where: str, exc: BaseException) -> None:
    """The kind and this script's line only -- never the message text."""
    lines = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename in ("<stdin>", __file__)]
    print(f"  {where}: not run ({type(exc).__name__} at line {lines[-1] if lines else '?'})")


def days_left(expiry: date, at: datetime, trading: bool) -> float:
    """To `expiry`'s 15:30: calendar days, or trading sessions (what's left of
    today's 09:15-15:30 plus each trading day after it)."""
    if not trading:
        return max((datetime.combine(expiry, EXPIRY_CLOSE, IST) - at).total_seconds() / 86400, 0.0)
    open_, close = datetime.combine(at.date(), FIRST_RECORD, IST), datetime.combine(at.date(), EXPIRY_CLOSE, IST)
    left = 0.0
    if is_trading_day(at.date()):
        left = min(max((close - max(at, open_)).total_seconds() / (close - open_).total_seconds(), 0.0), 1.0)
    d = at.date() + timedelta(days=1)
    while d <= expiry:
        left += 1.0 if is_trading_day(d) else 0.0
        d += timedelta(days=1)
    return left


class Market:
    """The stored records and prices, loaded once and shared by every run."""

    def __init__(self, db):
        self.db = db
        self.by_day: dict[date, list[tuple[dtime, PcrSnapshot]]] = {}
        self.by_ts: dict[datetime, PcrSnapshot] = {}
        self.expiries: list[date] = []
        self.month_last: set[date] = set()
        self.px: dict[tuple, tuple[float | None, float | None]] = {}  # (record id, expiry, strike, type) -> (oi, last price)
        self.loaded: set[tuple] = set()
        self.symbols: dict[tuple, tuple] = {}
        self.bars: dict[tuple, dict[dtime, float]] = {}
        self.source = Counter()
        self.lot = cal.DEFAULT_LOT_SIZE

    async def load(self, before: date) -> None:
        snaps = (await self.db.execute(
            select(PcrSnapshot).where(PcrSnapshot.underlying == UNDERLYING, PcrSnapshot.session_date < before).order_by(PcrSnapshot.ts)
        )).scalars().all()
        known: set[date] = set()
        for s in snaps:
            self.by_ts[as_aware_utc(s.ts)] = s
            t = ist(s.ts).time()
            if FIRST_RECORD <= t <= CLOSE_RECORD:
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

    @property
    def sessions(self) -> list[date]:
        return sorted(d for d, recs in self.by_day.items() if any(t >= cal.ENTRY_TIME for t, _ in recs))

    def nearest(self, d: date) -> date | None:
        return next((e for e in self.expiries if e >= d), None)

    def weekly_for(self, d: date) -> date | None:
        """As the card: the nearest expiry, next week's on an expiry day."""
        es = [e for e in self.expiries if e >= d]
        if not es:
            return None
        return es[1] if es[0] == d and len(es) > 1 else (es[0] if es[0] != d else None)

    def monthly_after(self, weekly: date) -> date | None:
        return next((e for e in self.expiries if e > weekly and e in self.month_last), None)

    async def load_day(self, d: date) -> None:
        recs = [s for _, s in self.by_day.get(d, [])]
        if not recs:
            return
        ids = {s.id for s in recs}
        for s in recs:
            base = self.by_ts.get(as_aware_utc(s.day_baseline_ts)) if s.day_baseline_ts else None
            if base is not None:
                ids.add(base.id)
        need = set()
        for w in (self.nearest(d), self.weekly_for(d)):
            if w:
                need.add(w)
                if (m := self.monthly_after(w)):
                    need.add(m)
        spots = [s.spot for s in recs if s.spot]
        if not spots or not need:
            return
        lo, hi = min(spots) - STRIKE_PAD, max(spots) + STRIKE_PAD
        rows = (await self.db.execute(
            select(PcrStrikeOi.snapshot_id, PcrStrikeOi.expiry, PcrStrikeOi.strike, PcrStrikeOi.option_type, PcrStrikeOi.oi,
                   PcrStrikeOi.last_price)
            .where(PcrStrikeOi.snapshot_id.in_(ids), PcrStrikeOi.expiry.in_(need), PcrStrikeOi.strike.between(lo, hi))
        )).all()
        for sid, e, k, t, oi, lp in rows:
            self.px[(sid, e, float(k), t)] = (oi, lp)
        for e in need:
            self.loaded.add((d, e, "range", lo, hi))

    def _in_range(self, d: date, e: date, k: float) -> bool:
        return any(key[0] == d and key[1] == e and key[3] <= k <= key[4] for key in self.loaded if key[2] == "range")

    async def _lazy(self, d: date, e: date, k: float) -> None:
        """A contract outside what the day loaded (a carried strike far from NIFTY)."""
        if (d, e, k) in self.loaded or self._in_range(d, e, k):
            return
        self.loaded.add((d, e, k))
        ids = [s.id for _, s in self.by_day.get(d, [])]
        rows = (await self.db.execute(
            select(PcrStrikeOi.snapshot_id, PcrStrikeOi.option_type, PcrStrikeOi.oi, PcrStrikeOi.last_price)
            .where(PcrStrikeOi.snapshot_id.in_(ids), PcrStrikeOi.expiry == e, PcrStrikeOi.strike == k)
        )).all()
        for sid, t, oi, lp in rows:
            self.px[(sid, e, k, t)] = (oi, lp)

    async def price(self, d: date, t: dtime, rec: PcrSnapshot, e: date, k: float, typ: str) -> float | None:
        found = self.px.get((rec.id, e, float(k), typ))
        if found is None:
            await self._lazy(d, e, float(k))
            found = self.px.get((rec.id, e, float(k), typ))
        if found is not None and found[1]:
            self.source["record"] += 1
            return float(found[1])
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
            begin = (datetime.combine(d, t) - timedelta(minutes=15)).time()
            close = self.bars[(sid, d)].get(begin)
            if close:
                self.source["candle"] += 1
                return close
        self.source["none"] += 1
        return None

    def oi_added(self, rec: PcrSnapshot, weekly: date, strikes: list[float]) -> dict[float, float] | None:
        base = self.by_ts.get(as_aware_utc(rec.day_baseline_ts)) if rec.day_baseline_ts else None
        if base is None:
            return None
        added = {}
        for k in strikes:
            parts = [(self.px.get((rec.id, weekly, float(k), t)), self.px.get((base.id, weekly, float(k), t))) for t in ("CE", "PE")]
            if all(a and b and a[0] is not None and b[0] is not None for a, b in parts):
                added[float(k)] = sum(a[0] - b[0] for a, b in parts)
        return added or None


async def open_at(m: Market, rule: Rule, d: date, t: dtime, rec: PcrSnapshot) -> tuple[dict | None, str]:
    weekly = m.weekly_for(d)
    monthly = m.monthly_after(weekly) if weekly else None
    if weekly is None or monthly is None or not rec.spot:
        return None, "expiry"
    spot = rec.spot
    contracts = {}
    for k in cal.candidate_strikes(spot):
        ce, pe = await m.price(d, t, rec, weekly, k, "CE"), await m.price(d, t, rec, weekly, k, "PE")
        if ce and pe:
            contracts[float(k)] = (ce, pe)
    if not contracts:
        return None, "price"
    strike, how = None, "pcr"
    if rule.strike_rule == "oi":
        added = m.oi_added(rec, weekly, list(contracts))
        if added:
            strike, how = cal.most_added(added, spot), "oi"
    if strike is None and rec.pcr is not None:
        strike = cal.pick_strike(contracts, cal.side_for(rec.pcr), spot)
    if strike is None:
        return None, "strike"
    wce, wpe = contracts[strike]
    mce, mpe = await m.price(d, t, rec, monthly, strike, "CE"), await m.price(d, t, rec, monthly, strike, "PE")
    if not (mce and mpe):
        return None, "price"
    at = datetime.combine(d, t, IST)
    ratio = (mce + mpe) / (wce + wpe)
    wc, mc = days_left(weekly, at, False), days_left(monthly, at, False)
    wt, mt = days_left(weekly, at, True), days_left(monthly, at, True)
    rel_c = ratio / math.sqrt(mc / wc) if wc > 0 and mc > 0 else None
    rel_t = ratio / math.sqrt(mt / wt) if wt > 0 and mt > 0 else None
    if rule.ratio:
        rel = rel_c if rule.ratio[0] == "calendar" else rel_t
        if rel is None or rel > rule.ratio[1]:
            return None, "filter"
    if rule.weekly:
        op, n = rule.weekly
        if (op == "le" and wt > n) or (op == "gt" and wt <= n):
            return None, "filter"
    qty = float(cal.LOTS_PER_LEG * m.lot)
    legs, entry_charges = {}, 0.0
    mid = {"short_ce": wce, "short_pe": wpe, "long_ce": mce, "long_pe": mpe}
    for name, option_type, side, which in cal.LEGS:
        price = bid(mid[name]) if side == "sell" else ask(mid[name])
        legs[name] = {"option_type": option_type, "side": side, "quantity": qty, "entry_price": price,
                      "expiry": weekly if which == "weekly" else monthly, "last_seen": mid[name]}
        entry_charges += cal.charges_of(legs[name], price, buying=side == "buy")
    return {"strike": strike, "weekly": weekly, "monthly": monthly, "legs": legs, "entry_charges": entry_charges,
            "opened": at, "how": how, "rel_c": rel_c, "rel_t": rel_t, "wt": wt, "entry_spot": spot, "checked_on": d}, "open"


async def run(m: Market, rule: Rule) -> dict:
    trades, passed, nights = [], Counter(), 0
    position = None
    last_rec = None

    def close(reason: str, prices: dict[str, float], d: date, t: dtime, booked: float | None = None) -> None:
        nonlocal position
        gross = sum(cal.leg_pnl(leg, prices[n]) for n, leg in position["legs"].items())
        exit_charges = sum(cal.charges_of(leg, prices[n], buying=leg["side"] == "sell") for n, leg in position["legs"].items())
        net = gross - position["entry_charges"] - exit_charges if booked is None else booked
        trades.append({"opened": position["opened"], "closed": datetime.combine(d, t, IST), "reason": reason, "net": net,
                       "gross": gross, "how": position["how"], "nights": position.get("nights", 0)})
        position = None

    def forced(position: dict) -> dict[str, float]:
        return {n: cal.closing_price(leg, bid(leg["last_seen"]), ask(leg["last_seen"])) for n, leg in position["legs"].items()}

    for d in m.sessions:
        await m.load_day(d)
        day_done = reopen_due = False
        if position is not None:
            nights += 1
            position["nights"] = position.get("nights", 0) + 1
        for t, rec in m.by_day[d]:
            if t < cal.ENTRY_TIME and position is None:
                continue  # 09:15: only a carried position is checked (its take-profit, at the open's prices)
            last_rec = (d, t, rec)
            if position is not None:
                quotes = {}
                for n, leg in position["legs"].items():
                    p = await m.price(d, t, rec, leg["expiry"], position["strike"], leg["option_type"])
                    if p:
                        leg["last_seen"] = p
                        quotes[n] = (bid(p), ask(p))
                net = prices = None
                if len(quotes) == 4:
                    net, _, prices = cal.if_closed_now(position, quotes)
                    if net >= cal.TAKE_PROFIT:
                        # checked earlier today: the card, checking every second, closes as it reaches the target --
                        # booked at the target; the first check of a day (an overnight gap) at what it is
                        close("take_profit", prices, d, t, booked=cal.TAKE_PROFIT if position["checked_on"] == d else None)
                        day_done = day_done or t >= cal.DAY_DONE_AFTER
                    else:
                        position["checked_on"] = d
                if position is not None:
                    weekly = position["weekly"]
                    if weekly < d or (weekly == d and t >= cal.ROLL_TIME):
                        if prices is None:
                            prices = forced(position)
                            net = cal.net_at(position, prices)
                        if net < 0:
                            close("expiry_roll", prices, d, t)
                            reopen_due = True
                        else:
                            close("weekly_expiry_3pm", prices, d, t)
                            day_done = True
                    elif t >= CLOSE_RECORD:
                        if net is not None and net >= 0:
                            close(cal.DAILY_CLOSE_REASON, prices, d, t)
                    elif t < cal.ENTRY_TIME:
                        pass  # 09:15: the take-profit only
                    elif rule.drift and net is not None and rec.spot and abs(
                            rec.spot - (position["strike"] if rule.drift_from == "strike" else position["entry_spot"])) >= rule.drift:
                        close("drift", prices, d, t)
                        day_done = day_done or t >= cal.DAY_DONE_AFTER
            if position is None:
                allowed = reopen_due or (cal.ENTRY_TIME <= t < rule.entry_until and not day_done)
                if allowed:
                    opened, why = await open_at(m, rule, d, t, rec)
                    if opened is not None:
                        position, reopen_due = opened, False
                    else:
                        passed[why] += 1
    open_left = None
    if position is not None and last_rec is not None:  # still open at the end: valued at the last record
        d, t, rec = last_rec
        open_left = cal.net_at(position, forced(position))
    return {"rule": rule, "trades": trades, "passed": passed, "nights": nights, "open_left": open_left}


def stats(result: dict, halves: tuple[date, date], units: float) -> dict:
    trades = result["trades"]
    nets = [t["net"] for t in trades]
    total = sum(nets) + (result["open_left"] or 0.0)
    peak = run_ = dd = 0.0
    for x in nets:
        run_ += x
        peak = max(peak, run_)
        dd = max(dd, peak - run_)
    mid = halves[1]
    first = sum(t["net"] for t in trades if t["closed"].date() < mid)
    second = sum(t["net"] for t in trades if t["closed"].date() >= mid) + (result["open_left"] or 0.0)
    by_day = Counter(t["closed"].date() for t in trades if t["reason"] == "take_profit")
    return {
        "n": len(trades), "tp": sum(1 for t in trades if t["reason"] == "take_profit"), "wins": sum(1 for x in nets if x > 0),
        "net": total, "first": first, "second": second, "dd": dd, "worst": min(nets, default=0.0),
        "avg": (sum(nets) / len(nets)) if nets else 0.0, "units": units, "open_left": result["open_left"],
        "reasons": Counter(t["reason"] for t in trades), "nights": result["nights"],
        "multi_tp_days": sum(1 for n in by_day.values() if n >= 2),
        "held": sorted((t["closed"] - t["opened"]).total_seconds() / 60 for t in trades),
        "by_oi": sum(1 for t in trades if t["how"] == "oi"), "passed": result["passed"],
    }


def line(name: str, s: dict) -> str:
    u = s["units"]
    held = s["held"][len(s["held"]) // 2] if s["held"] else 0
    return (f"  {name:<44} {s['n']:>3} trades, {s['tp']:>3} TP ({s['multi_tp_days']} days with 2+), "
            f"win {100 * s['wins'] / s['n'] if s['n'] else 0:>3.0f}%; net {s['net'] / u:+8.1f} pts "
            f"(1st half {s['first'] / u:+7.1f}, 2nd {s['second'] / u:+7.1f}); avg {s['avg'] / u:+5.1f}, worst {s['worst'] / u:+6.1f}, "
            f"max drawdown {s['dd'] / u:5.1f}; nights carried {s['nights']}; median hold {held:,.0f} min"
            + (f"; still open {s['open_left'] / u:+.1f}" if s["open_left"] is not None else ""))


def rupees(name: str, s: dict) -> str:
    return (f"{name}: net Rs {s['net']:+,.0f} (1st half {s['first']:+,.0f}, 2nd {s['second']:+,.0f}); {s['n']} trades, "
            f"{s['tp']} TP; worst {s['worst']:+,.0f}; max drawdown {s['dd']:,.0f}")


async def main() -> None:
    today = datetime.now(timezone.utc).astimezone(IST).date()
    singles = [
        Rule("baseline: version 13 (strike by OI added)"),
        Rule("baseline: version 12 (strike by PCR)", strike_rule="pcr"),
        *(Rule(f"drift: close at {x:.0f} pts from the strike", drift=x, family="drift") for x in (100.0, 150.0, 200.0)),
        *(Rule(f"drift: close at {x:.0f} pts from NIFTY at entry", drift=x, drift_from="entry", family="drift")
          for x in (100.0, 150.0, 200.0)),
        *(Rule(f"cut-off: no new position from {t:%H:%M}", entry_until=t, family="cut-off") for t in (dtime(11, 30), dtime(12, 30))),
        *(Rule(f"ratio: {basis} at or below {x:.2f}", ratio=(basis, x), family="ratio")
          for basis in ("calendar", "trading") for x in (0.85, 0.90, 0.95)),
        Rule("weekly: 2 trading sessions or less left", weekly=("le", 2.0), family="weekly"),
        Rule("weekly: more than 2 trading sessions left", weekly=("gt", 2.0), family="weekly"),
    ]
    print("Rules tested (the public log: points per unit after charges and the spread; rupees to the private Telegram chat):")
    print("  baseline version 13 -- from 09:30 the weekly CE+PE sold, the monthly after it bought, at the strike with the most")
    print("    weekly CE+PE OI added since the previous close (PCR premium rule without a record); 20 lots; take-profit Rs 14,000")
    print("    after charges at bid/ask; next one after a close, none after a close from 14:00; new ones until 15:00; 15:29 closed")
    print("    in profit, carried in loss; weekly expiry day 15:00 closed (in loss rolled into a new one)")
    print(f"  priced at each 15-minute record's last price (the candle's close where none), +-{100 * HALF_SPREAD:.1f}% for the bid/ask,")
    print("    checked at each record only; the 15:30 record stands for 15:29")
    for r in singles[2:]:
        print(f"  + {r.name}")
    async with AsyncSessionLocal() as db:
        m = Market(db)
        await m.load(today)
        sessions = m.sessions
        if not sessions:
            print("no sessions with records")
            return
        halves = (sessions[0], sessions[len(sessions) // 2])
        print()
        print(f"sessions: {len(sessions)} ({sessions[0]:%d %b} - {sessions[-1]:%d %b}; 2nd half from {halves[1]:%d %b}); "
              f"records: {sum(len(v) for v in m.by_day.values())}; expiries known: {len(m.expiries)}; lot {m.lot}")
        units = float(cal.LOTS_PER_LEG * m.lot)
        results: dict[str, dict] = {}
        for r in singles:
            try:
                results[r.name] = {"rule": r, "s": stats(await run(m, r), halves, units)}
            except Exception as exc:  # noqa: BLE001
                failed(r.name, exc)
        print(f"prices read: {m.source['record']} from records, {m.source['candle']} from candles, {m.source['none']} not found")

        best: dict[str, Rule] = {}
        for name, x in results.items():
            fam = x["rule"].family
            if fam != "baseline" and (fam not in best or x["s"]["net"] > results[best[fam].name]["s"]["net"]):
                best[fam] = x["rule"]
        combos = []
        for a, b in itertools.combinations(sorted(best), 2):
            ra, rb = best[a], best[b]
            combos.append(replace(ra, name=f"{ra.name} + {rb.name}", family="pair", **{
                k: getattr(rb, k) for k in ("drift", "drift_from", "entry_until", "ratio", "weekly") if getattr(rb, k) != getattr(Rule(""), k)}))
        if len(best) > 2:
            allr = Rule("all four best", family="all")
            for fam in sorted(best):
                allr = replace(allr, **{k: getattr(best[fam], k) for k in ("drift", "drift_from", "entry_until", "ratio", "weekly")
                                        if getattr(best[fam], k) != getattr(Rule(""), k)})
            combos.append(allr)
        for r in combos:
            try:
                results[r.name] = {"rule": r, "s": stats(await run(m, r), halves, units)}
            except Exception as exc:  # noqa: BLE001
                failed(r.name, exc)

        base = results.get(singles[0].name)
        print()
        print("One rule at a time, then the best of each family together (net per unit, after charges and the spread):")
        for r in [*singles, *combos]:
            if r.name in results:
                print(line(r.name, results[r.name]["s"]))
        print()
        print("Exits (baseline, then each family's best):")
        for name in [singles[0].name, *(best[f].name for f in sorted(best))]:
            if name in results:
                s = results[name]["s"]
                print(f"  {name}: " + ", ".join(f"{k} x{v}" for k, v in s["reasons"].most_common())
                      + f"; strike by OI added {s['by_oi']} of {s['n']}; entries passed over: "
                      + (", ".join(f"{k} x{v}" for k, v in s["passed"].most_common()) or "none"))
        if base:
            b = base["s"]
            beat = [name for name, x in results.items() if x["rule"].family != "baseline"
                    and x["s"]["first"] > b["first"] and x["s"]["second"] > b["second"]]
            print()
            print(f"beat the baseline in both halves: {len(beat)} of {len(results) - 2}" + (": " + "; ".join(beat) if beat else ""))

    lines = [rupees(name, x["s"]) for name, x in results.items()]
    await send_telegram("Conti Calendar 15 MIN: rules backtest (private)",
                        f"{len(sessions)} sessions ({sessions[0]:%d %b} - {sessions[-1]:%d %b}), 20 lots a leg, 15-minute records, "
                        f"0.1% half-spread and charges:\n\n" + "\n\n".join(lines))
    print("sent to the private Telegram chat: rupees for every rule, yes")


if __name__ == "__main__":
    asyncio.run(main())
