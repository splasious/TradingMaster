"""Conti Calendar 400 15 MIN: version 8 against version 9, and how often the chart-peak shift fires (26 Nov).

Read-only. Asked 26 Nov, before version 9 goes live: "Backtest how often the
peak shift would fire", then "Which setup is better on backtesting, the
current existing set up or if shifting done on bi weekly and shifting at
chart peak". Replayed over every NIFTY session with 15-minute records
(pcr_snapshots) before today: version 8 (today's, V8), version 9 with the
chart-peak shift (A, as built) and without it (B).

Version 8 (V8):

  entry    from 09:30 to 15:00 whenever none is open, and at once after a
           monthly roll: sell the nearest weekly (next week's on an expiry
           day; the expiry after it when it has no OTM strike bid Rs 100 on
           a side) at the furthest OTM 50-point strike on each side bid
           Rs 100 or more; on each side buy the nearest monthly after it at
           the strike whose ask is 2.5-3x the weekly's bid, nearest 3x
  target   and shifts as version 9's below, but without the chart peak; a
           shifted side's monthly re-picked by the same ratio (kept when
           the same strike)
  roll     15:00 on the sold weekly's expiry day: the sold legs to next
           week's at the same strikes; when next week's is the monthly's
           own (the month's last week), all four closed and a fresh one
           opened at once

Version 9 (A; B the same without the chart-peak shift):

  entry    from 09:30 to 15:00 whenever none is open, and at once after a
           hedge roll: sell the nearest weekly whose roll day (the trading
           day before it) is still to come -- the expiry after it when it
           has no OTM strike bid Rs 100 or more on a side -- at the furthest
           OTM 50-point strike on each side whose bid is still Rs 100 or
           more; buy the listed expiry 11-17 days after it (nearest 14) at
           the same strikes; 20 lots a leg
  target   Rs 14,000 after charges at bid/ask, counted from the last shift
  shifts   a side shifted when NIFTY is past its reference (NIFTY when the
           side was opened or last shifted) by 150 points in loss or 200 in
           profit -- or (A only) at or past that side's own profit-chart
           peak: that side's two legs' profit at the roll, each leg at
           Black-Scholes with its implied volatility now, closed at bid/ask
           less charges (the strategy's chart_peaks); the side reopened at
           NIFTY's Rs 100 strike now, its hedge at the same strike, the same
           expiries; the closed legs a trade of their own. Only 09:30-15:00.
  roll     15:00 on the trading day before the sold weekly's expiry: the
           sold legs bought back and the next weekly sold at the same
           strikes, the hedges kept; when that next weekly is the hedges'
           own, all four closed and a fresh one opened at once

How it is priced: each 15-minute record's per-contract last price
(pcr_strike_oi; the stored 15-minute candle's close where a record has
none), a sell at the price less HALF_SPREAD, a buy at it plus (the bid/ask);
the card's charges on every order. Checked only at each record (the card
checks every second): a target seen at a record after an earlier check that
day is booked at the target itself (the card closes as it gets there), the
first check of a day at what it is.

The public log gets the rules, counts, NIFTY levels and strikes, and results
in points per unit (net rupees / the 1,300 units a leg) -- no rupees. Run
inside the backend container (the health check's `cc400_peak_backtest`
input pipes it to `python -`). Self-contained: the app running there may
still be on version 8.
"""

import asyncio
import math
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.pcr import PcrSnapshot, PcrStrikeOi
from app.services.backfill_platform.coverage import previous_trading_day
from app.services.broker.zerodha_broker import IST
from app.services.options.calendar_payoff import PayoffLeg, calendar_payoff
from app.services.paper_trading.trade_record import order_charges

UNDERLYING = "NIFTY"
HALF_SPREAD = 0.001  # 0.1% of the price each way, as the other calendar backtests
STRIKE_PAD = 800.0  # points either side of the session's NIFTY range loaded up front
LOOKAHEAD = timedelta(days=70)  # expiries loaded each session: the sold, the next, two weeks past either, a monthly after them
FIRST_RECORD, CLOSE_RECORD = dtime(9, 15), dtime(15, 30)
ENTRY_TIME, ENTRY_UNTIL, ROLL_TIME = dtime(9, 30), dtime(15, 0), dtime(15, 0)
STRIKE_STEP, MIN_PREMIUM, MAX_STEPS = 50, 100.0, 40
HEDGE_DAYS = (11, 17)
MONTHLY_RATIO = (2.5, 3.0)  # version 8: the bought monthly's ask over the sold weekly's bid, nearest the top
LOTS_PER_LEG, DEFAULT_LOT = 20, 65
SHIFT_IN_LOSS, SHIFT_IN_PROFIT = 150.0, 200.0
TAKE_PROFIT = 14_000.0
SIDES = {"CE": ("short_ce", "long_ce"), "PE": ("short_pe", "long_pe")}
SIDE_NAME = {"CE": "call", "PE": "put"}


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


def first_otm(spot: float, option_type: str) -> float:
    if option_type == "CE":
        return (math.floor(spot / STRIKE_STEP) + 1) * STRIKE_STEP
    return (math.ceil(spot / STRIKE_STEP) - 1) * STRIKE_STEP


def roll_moment(weekly: date) -> datetime:
    return datetime.combine(previous_trading_day(weekly), ROLL_TIME, tzinfo=IST)


def hedge_after(expiries: list[date], weekly: date) -> date | None:
    lo, hi = HEDGE_DAYS
    near = [e for e in expiries if lo <= (e - weekly).days <= hi]
    return min(near, key=lambda e: abs((e - weekly).days - 14)) if near else None


def closing(leg: dict, mid: float) -> float:
    return ask(mid) if leg["side"] == "sell" else bid(mid)


def leg_net(leg: dict, mid: float) -> float:
    """Closing `leg` at `mid`'s bid/ask: its P&L less its opening and closing charges."""
    price = closing(leg, mid)
    diff = leg["entry"] - price if leg["side"] == "sell" else price - leg["entry"]
    return diff * leg["qty"] - leg["entry_charges"] - charges(leg["type"], leg["qty"], price, buying=leg["side"] == "sell")


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


class Run:
    def __init__(self, m: Market, version: int, peak_shift: bool, name: str):
        self.m, self.version, self.peak_shift, self.name = m, version, peak_shift, name
        self.qty = float(LOTS_PER_LEG * m.lot)
        self.position: dict | None = None
        self.reopen_due = False
        self.trades: list[dict] = []
        self.shifts: list[dict] = []
        self.touches: list[tuple] = []  # B: records where NIFTY was at or past a side's peak (no shift for it)
        self.rolls = 0
        self.waits = Counter()
        self.open_left: float | None = None

    # -- the position --------------------------------------------------------------------------------------------
    def new_leg(self, typ: str, side: str, expiry: date, strike: float, mid: float) -> dict:
        price = bid(mid) if side == "sell" else ask(mid)
        return {"type": typ, "side": side, "expiry": expiry, "strike": strike, "qty": self.qty, "entry": price, "last": mid,
                "entry_charges": charges(typ, self.qty, price, buying=side == "buy")}

    async def mids(self, d: date, t: dtime, rec) -> dict[str, float]:
        out = {}
        for name, leg in self.position["legs"].items():
            p = await self.m.price(d, t, rec, leg["expiry"], leg["strike"], leg["type"])
            if p:
                leg["last"] = out[name] = p
        return out

    def figure(self) -> float:
        """What the target reads: rolls booked since the last shift plus the open legs closed now."""
        p = self.position
        return p["booked"] - p["outside"] + sum(leg_net(leg, leg["last"]) for leg in p["legs"].values())

    def close_all(self, reason: str, d: date, t: dtime, at_target: bool = False) -> None:
        p = self.position
        net = p["booked"] + sum(leg_net(leg, leg["last"]) for leg in p["legs"].values())
        if at_target:
            net -= self.figure() - TAKE_PROFIT  # the card closes as it reaches the target
        self.trades.append({"kind": reason, "net": net, "opened": p["opened"], "closed": datetime.combine(d, t, IST)})
        self.position = None

    async def side_strike(self, d, t, rec, expiry: date, spot: float, typ: str) -> tuple[float | None, str]:
        found, step = None, STRIKE_STEP if typ == "CE" else -STRIKE_STEP
        for k in range(MAX_STEPS):
            strike = first_otm(spot, typ) + k * step
            p = await self.m.price(d, t, rec, expiry, strike, typ)
            if p is None:
                if k == 0:
                    return None, "no_book"
                break  # past what's recorded: the last one in
            if bid(p) < MIN_PREMIUM:
                break
            found = strike
        return (found, "") if found is not None else (None, "no_100")

    def hedge_for(self, listed: list[date], weekly: date | None) -> date | None:
        if weekly is None:
            return None
        return hedge_after(listed, weekly) if self.version == 9 else self.m.monthly_after(weekly)

    async def monthly_cover(self, d, t, rec, monthly: date, typ: str, weekly_strike: float, weekly_bid: float) -> tuple | None:
        """Version 8: (strike, mid) of the monthly whose ask is 2.5-3x `weekly_bid`, nearest 3x -- the strategy's walk."""
        lo, hi = MONTHLY_RATIO
        otm = STRIKE_STEP if typ == "CE" else -STRIKE_STEP
        seen: dict[float, tuple | None] = {}

        async def at(k: float):
            if k not in seen:
                p = await self.m.price(d, t, rec, monthly, k, typ)
                seen[k] = None if p is None else (ask(p) / weekly_bid, p)
            return seen[k]

        k = weekly_strike
        r = await at(k)
        if r is None:
            return None
        direction = otm if r[0] > hi else -otm
        best = (k, r) if lo <= r[0] <= hi else None
        for _ in range(MAX_STEPS):
            if best is not None and direction == otm:
                break
            nk = k + direction
            nr = await at(nk)
            if nr is None:
                break
            if lo <= nr[0] <= hi and best is not None and nr[0] <= best[1][0]:
                break
            k, r = nk, nr
            if lo <= r[0] <= hi:
                best = (k, r)
            elif best is not None or (direction == otm and r[0] < lo) or (direction != otm and r[0] > hi):
                break
        return (best[0], best[1][1]) if best is not None else None

    async def open_position(self, d: date, t: dtime, rec) -> bool:
        listed = self.m.listed(d)
        if self.version == 9:
            weekly = next((e for e in listed if previous_trading_day(e) > d), None)
        else:  # the nearest, next week's on an expiry day
            weekly = (listed[1] if len(listed) > 1 else None) if listed and listed[0] == d else (listed[0] if listed else None)
        hedge = self.hedge_for(listed, weekly)
        if hedge is None:
            self.waits["no hedge expiry"] += 1
            return False
        spot = rec.spot

        async def strikes_on(expiry: date):
            out = {}
            for typ in ("CE", "PE"):
                k, why = await self.side_strike(d, t, rec, expiry, spot, typ)
                if k is None:
                    return None, why
                out[typ] = k
            return out, ""

        strikes, why = await strikes_on(weekly)
        if strikes is None and why == "no_100":
            later = next((e for e in listed if e > weekly), None)
            later_hedge = self.hedge_for(listed, later)
            if later_hedge is not None:
                strikes, why = await strikes_on(later)
                if strikes is not None:
                    weekly, hedge = later, later_hedge
                    self.waits["next expiry sold (version 4)"] += 1
        if strikes is None:
            self.waits[f"entry: {why}"] += 1
            return False
        legs = {}
        for typ, (short, long_) in SIDES.items():
            wp = await self.m.price(d, t, rec, weekly, strikes[typ], typ)
            if wp is None:
                self.waits["entry: a leg unpriced"] += 1
                return False
            if self.version == 9:  # the hedge at the same strike
                hk, hp = strikes[typ], await self.m.price(d, t, rec, hedge, strikes[typ], typ)
            else:  # the monthly 2.5-3x the weekly's bid
                cover = await self.monthly_cover(d, t, rec, hedge, typ, strikes[typ], bid(wp))
                hk, hp = cover if cover is not None else (None, None)
            if hp is None:
                self.waits["entry: no hedge priced" + (" / no 2.5-3x cover" if self.version == 8 else "")] += 1
                return False
            legs[short] = self.new_leg(typ, "sell", weekly, strikes[typ], wp)
            legs[long_] = self.new_leg(typ, "buy", hedge, hk, hp)
        self.position = {"legs": legs, "weekly": weekly, "hedge": hedge, "booked": 0.0, "outside": 0.0,
                         "refs": {"CE": spot, "PE": spot}, "opened": datetime.combine(d, t, IST), "checked_on": None}
        return True

    def peaks(self, spot: float, now: datetime) -> dict[str, float]:
        p, out = self.position, {}
        horizon = roll_moment(p["weekly"])
        for typ, names in SIDES.items():
            legs = [PayoffLeg(name=n, option_type=typ, strike=float(p["legs"][n]["strike"]), expiry=p["legs"][n]["expiry"],
                              side=p["legs"][n]["side"], quantity=self.qty, entry_price=p["legs"][n]["entry"],
                              mid=p["legs"][n]["last"], half_spread=p["legs"][n]["last"] * HALF_SPREAD) for n in names]
            payoff = calendar_payoff(legs, spot, now.astimezone(timezone.utc), horizon, 0.0,
                                     lambda leg, price, buying: charges(leg.option_type, leg.quantity, price, buying))
            if payoff is None:
                continue
            i = max(range(len(payoff.points)), key=lambda j: payoff.points[j][1])
            if 0 < i < len(payoff.points) - 1:
                out[typ] = payoff.points[i][0]
        return out

    async def shift(self, d, t, rec, typ: str, by: str, figure: float, peak: float | None) -> bool:
        p, spot = self.position, rec.spot
        short, long_ = SIDES[typ]
        old = p["legs"][short]["strike"]
        strike, why = await self.side_strike(d, t, rec, p["weekly"], spot, typ)
        if strike is None:
            self.waits[f"shift waits: {why}"] += 1
            return False
        if strike == old:
            p["refs"][typ] = spot
            self.waits[f"shift by {by}: the Rs 100 strike unmoved (reference moved)"] += 1
            return False
        wp = await self.m.price(d, t, rec, p["weekly"], strike, typ)
        hk, hp = strike, None
        if wp is not None and self.version == 9:
            hp = await self.m.price(d, t, rec, p["hedge"], strike, typ)
        elif wp is not None:
            cover = await self.monthly_cover(d, t, rec, p["hedge"], typ, strike, bid(wp))
            hk, hp = cover if cover is not None else (None, None)
        if wp is None or hp is None:
            self.waits["shift waits: a new leg unpriced" + (" / no 2.5-3x cover" if self.version == 8 else "")] += 1
            return False
        replaced = (short, long_) if self.version == 9 or hk != p["legs"][long_]["strike"] else (short,)  # v8: the same cover kept
        closed = sum(leg_net(p["legs"][n], p["legs"][n]["last"]) for n in replaced)
        self.trades.append({"kind": "strike_shift", "net": closed, "opened": p["opened"], "closed": datetime.combine(d, t, IST)})
        p["legs"][short] = self.new_leg(typ, "sell", p["weekly"], strike, wp)
        if long_ in replaced:
            p["legs"][long_] = self.new_leg(typ, "buy", p["hedge"], hk, hp)
        ref = p["refs"][typ]
        p["refs"][typ], p["outside"] = spot, p["booked"]
        self.shifts.append({"at": datetime.combine(d, t, IST), "side": typ, "by": by, "spot": spot, "ref": ref, "peak": peak,
                            "old": old, "new": strike, "closed": closed, "in_loss": figure < 0})
        return True

    async def roll(self, d: date, t: dtime, rec, now: datetime) -> None:
        p = self.position
        if self.version == 9:
            following = next((e for e in self.m.listed(d) if e > p["weekly"] and roll_moment(e) > now), None)
        else:  # never one expiring today
            following = next((e for e in self.m.listed(d) if e > d), None)
        if following is None:
            self.waits["roll: no later weekly"] += 1
            return
        if following >= p["hedge"]:
            self.close_all("hedge_roll" if self.version == 9 else "monthly_roll", d, t)
            self.reopen_due = True
            return
        fresh = {}
        for name in ("short_ce", "short_pe"):
            leg = p["legs"][name]
            q = await self.m.price(d, t, rec, following, leg["strike"], leg["type"])
            if q is None:
                self.waits["roll waits: next weekly unpriced"] += 1
                return
            fresh[name] = q
        for name, q in fresh.items():
            leg = p["legs"][name]
            p["booked"] += leg_net(leg, leg["last"])
            p["legs"][name] = self.new_leg(leg["type"], "sell", following, leg["strike"], q)
        p["weekly"] = following
        self.rolls += 1

    async def go(self) -> None:
        last = None
        for d in self.m.sessions:
            await self.m.load_day(d)
            for t, rec in self.m.by_day[d]:
                now = datetime.combine(d, t, IST)
                if self.position is None and t < ENTRY_TIME and not self.reopen_due:
                    continue
                last = (d, t)
                if self.position is not None:
                    mids = await self.mids(d, t, rec)
                    full = len(mids) == 4
                    figure = self.figure() if full else None
                    if full and figure >= TAKE_PROFIT:
                        self.close_all("take_profit", d, t, at_target=self.position["checked_on"] == d)
                    elif full:
                        self.position["checked_on"] = d
                if self.position is not None:
                    weekly = self.position["weekly"]
                    due = (now >= roll_moment(weekly) if self.version == 9
                           else weekly < d or (weekly == d and t >= ROLL_TIME))  # version 8: on the expiry day
                    if due:
                        await self.roll(d, t, rec, now)
                    elif full and ENTRY_TIME <= t < ENTRY_UNTIL:
                        trigger = SHIFT_IN_LOSS if figure < 0 else SHIFT_IN_PROFIT
                        peaks = self.peaks(rec.spot, now) if self.version == 9 else {}
                        for typ in ("CE", "PE"):
                            ref = self.position["refs"][typ]
                            moved = rec.spot - ref if typ == "CE" else ref - rec.spot
                            pk = peaks.get(typ)
                            at_peak = pk is not None and (rec.spot >= pk if typ == "CE" else rec.spot <= pk)
                            if moved > trigger:
                                by = "points"
                            elif at_peak and self.peak_shift:
                                by = "peak"
                            else:
                                if at_peak:
                                    self.touches.append((now, typ))
                                continue
                            if await self.shift(d, t, rec, typ, by, figure, pk):
                                break
                if self.position is None and (self.reopen_due or ENTRY_TIME <= t < ENTRY_UNTIL):
                    if await self.open_position(d, t, rec):
                        self.reopen_due = False
        if self.position is not None and last is not None:
            p = self.position
            self.open_left = p["booked"] + sum(leg_net(leg, leg["last"]) for leg in p["legs"].values())


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
    whole = [x["net"] for x in r.trades if x["kind"] != "strike_shift"]  # the positions' own trades
    by = Counter(s["by"] for s in r.shifts)
    sides = Counter((s["by"], s["side"]) for s in r.shifts)
    peak_days = len({s["at"].date() for s in r.shifts if s["by"] == "peak"})
    shift_net = sum(x["net"] for x in r.trades if x["kind"] == "strike_shift")
    print(f"{r.name}")
    print(f"  net {total / units:+.1f} pts a unit (1st half {first / units:+.1f}, 2nd {(total - first) / units:+.1f}); "
          f"max drawdown {dd / units:.1f}" + (f"; still open {r.open_left / units:+.1f}" if r.open_left is not None else ""))
    print(f"  closed: {kinds['take_profit']} at the target, {kinds['hedge_roll']} hedge rolls, {kinds['monthly_roll']} monthly rolls, "
          f"{kinds['strike_shift']} strike shifts (their closed legs {shift_net / units:+.1f} pts a unit); weekly rolls {r.rolls}")
    if whole:
        print(f"  positions closed: {len(whole)}, {sum(1 for x in whole if x > 0)} in profit; average {sum(whole) / len(whole) / units:+.1f} "
              f"pts, best {max(whole) / units:+.1f}, worst {min(whole) / units:+.1f} (each without its shifts' legs)")
    print(f"  shifts by the chart peak: {by['peak']} (call {sides[('peak', 'CE')]}, put {sides[('peak', 'PE')]}; on {peak_days} of "
          f"{len(sessions)} sessions); by 150/200 points: {by['points']} (call {sides[('points', 'CE')]}, put {sides[('points', 'PE')]})")
    if by["peak"]:
        moved = sorted(abs(s["spot"] - s["ref"]) for s in r.shifts if s["by"] == "peak")
        print(f"  NIFTY's move from the side's reference at a peak shift: median {moved[len(moved) // 2]:.0f} pts "
              f"(least {moved[0]:.0f}, most {moved[-1]:.0f}); in loss at {sum(s['in_loss'] for s in r.shifts if s['by'] == 'peak')}")
    if r.touches:
        print(f"  (not acted on) records with NIFTY at or past a side's chart peak: {len(r.touches)} on "
              f"{len({t.date() for t, _ in r.touches})} sessions")
    if r.waits:
        print("  waits: " + "; ".join(f"{k} {v}" for k, v in sorted(r.waits.items())))
    for s in r.shifts:
        print(f"    {s['at']:%d %b %H:%M} {SIDE_NAME[s['side']]} by {s['by']:<6} NIFTY {s['spot']:,.1f} (ref {s['ref']:,.1f}"
              + (f", peak {s['peak']:,.0f}" if s["peak"] is not None else "")
              + f") {s['old']:,.0f} -> {s['new']:,.0f}; closed legs {s['closed'] / units:+.1f} pts, {'in loss' if s['in_loss'] else 'in profit'}")


async def main() -> None:
    today = datetime.now(timezone.utc).astimezone(IST).date()
    print("Rules (points per unit after charges and the spread, no rupees here):")
    print("  V8 (today's): sell the nearest weekly (next week's on expiry day) at the Rs 100 strikes; buy the next monthly at the strike")
    print("    whose ask is 2.5-3x the weekly's bid; roll 15:00 on the expiry day; the month's last week all four afresh; shifts 150/200")
    print("  version 9 (A with the chart-peak shift, B without):")
    print("  entry 09:30-15:00 when none is open (at once after a hedge roll): sell the nearest weekly whose roll day is still to come")
    print("    (the next expiry when it has no OTM bid Rs 100 on a side) at the furthest OTM 50-pt strikes bid Rs 100+; buy the expiry")
    print("    11-17 days after it at the same strikes; 20 lots a leg; target Rs 14,000 after charges since the last shift")
    print("  shift a side at 150 pts past its reference in loss / 200 in profit, or (A) at its own profit-chart peak; to NIFTY's")
    print("    Rs 100 strike, the hedge at the same strike; 09:30-15:00. Roll 15:00 the day before expiry; every 2nd week all four afresh")
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
        for version, peak_shift, name in ((8, False, "V8: version 8, today's (monthly hedge 2.5-3x, roll on expiry day)"),
                                          (9, True, "A: version 9, the weekly hedge two weeks out and the chart-peak shift"),
                                          (9, False, "B: version 9 without the chart-peak shift")):
            r = Run(m, version, peak_shift, name)
            try:
                await r.go()
                report(r, sessions, units)
            except Exception as exc:  # noqa: BLE001
                failed(name, exc)
            print()
        print(f"prices read: {m.source['record']} from records, {m.source['candle']} from candles, {m.source['none']} not found")


if __name__ == "__main__":
    asyncio.run(main())
