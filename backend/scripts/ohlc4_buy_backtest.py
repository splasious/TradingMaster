"""OHLC4 Buy backtested before it goes live (27 Nov): the 15-minute version, and the 5-minute one deployed. Read-only.

Asked 27 Nov: "Backtest OHLC4 first". The new strategy
(native_strategies/nifty_ohlc4_buy.py, not deployed) replayed over every
whole NIFTY session with stored 15-minute NIFTY candles and NIFTY option
candles:

  the level  the previous 15-minute NIFTY candle's (O+H+L+C)/4; from 09:15
             to 09:30 the session before's last (15:15-15:30)
  the rule   NIFTY above the level: 1 lot of the ATM weekly call held; under
             it the put; a cross: the held option sold and the other bought
             (the 5 seconds between left out); recorded "stop_loss" when
             NIFTY is past the previous candle's low (a call) / high (a
             put), "level_flip" otherwise; no trailing stop
  carried    held overnight; on its expiry day rolled at 12:00 to next
             week's weekly, on the side NIFTY is on then
  prices     bought at the price + HALF_SPREAD (0.1%), sold at - 0.1%; the
             taxes and fees as live, no brokerage

Read two ways:

  every minute    NIFTY's finest stored candles (1-minute, else 5-minute)
                  against the level at each one's close, from the 09:15
                  open to 15:29 -- the nearest to the live per-second check
                  that stored data allows (the live flips at least as often).
                  A new candle's level counts from CANDLE_WAIT (a minute)
                  after its close, as the live waits 10-70 s for it. The
                  option inside its 15-minute candle is estimated: the
                  candle's open + DELTA (0.5) x NIFTY's move since (minus for
                  a put), kept inside the candle's high and low -- the
                  estimate is checked against the candles' own closes below
  15-min closes   only at each 15-minute open 09:15-15:15: NIFTY's open
                  against the previous candle's level; the option at its own
                  candle's open (no estimate)

Public log: counts, dates, NIFTY market data and points per unit only
(rupees: points x the lot of 65). Run inside the backend container (the
health check's `ohlc4_backtest` input pipes it to `python -`).
"""

import asyncio
import statistics
import traceback
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, or_, select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.pcr import PcrSnapshot
from app.services.backfill_platform.coverage import next_trading_day, previous_trading_day
from app.services.broker.zerodha_broker import IST
from app.services.paper_trading.trade_record import order_charges

SESSION_OPEN, LAST_BAR, DAY_CLOSE = time(9, 15), time(15, 15), time(15, 29)
BAR = timedelta(minutes=15)
BARS_A_DAY = 25  # 09:15 ... 15:15
FINE = {"1m": timedelta(minutes=1), "5m": timedelta(minutes=5)}
CANDLE_WAIT = timedelta(seconds=60)
STRIKE_STEP = 50
HALF_SPREAD = 0.001
DEFAULT_LOT = 65
DELTA = 0.5
ROLL_AT = time(12, 0)
SPOT_NAMES = ("NIFTY 50", "NSE:NIFTY 50")


def atm(spot: float) -> float:
    return float(int(spot / STRIKE_STEP + 0.5) * STRIKE_STEP)


def level_of(bar: dict) -> float:
    return (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4


def at(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=IST)


def bar_start(moment: datetime) -> datetime:
    """The 15-minute candle (09:15 grid) `moment` falls in."""
    first = at(moment.date(), SESSION_OPEN)
    return first + int((moment - first) / BAR) * BAR


def charges(option_type: str, qty: float, price: float, buying: bool, intraday: bool) -> float:
    return order_charges({"option_type": option_type, "quantity": qty}, price, buying, intraday, 0.0) or 0.0


def to_bar(o, h, lo, c) -> dict | None:
    if None in (o, h, lo, c):
        return None
    return {"open": float(o), "high": float(h), "low": float(lo), "close": float(c)}


class Data:
    """NIFTY's candles (15-minute and the finest stored), the listed expiries, the option candles."""

    def __init__(self, db):
        self.db = db
        self.spot: dict[str, dict[date, dict[time, dict]]] = {}
        self.expiries: dict[date, list[date]] = {}
        self.contracts: dict[tuple[date, float, str], tuple] = {}
        self.bars: dict[tuple, dict[time, dict]] = {}
        self.estimates: list[tuple[float, float, float]] = []  # (estimate error, |close - open|, realised delta) per option candle

    async def load(self) -> None:
        nifty = (await self.db.execute(select(Instrument.id).where(Instrument.symbol == "NIFTY 50"))).scalars().first()
        index_ids = (await self.db.execute(select(BfSymbol.id).where(
            BfSymbol.option_type.is_(None), BfSymbol.expiry.is_(None),
            or_(BfSymbol.symbol.in_(SPOT_NAMES), BfSymbol.display_name.in_(SPOT_NAMES)),
        ))).scalars().all()
        print("NIFTY 50 candles stored (bars, sessions, first -> last):")
        for timeframe in ("15m", *FINE):
            sources = {}
            if nifty is not None:
                rows = (await self.db.execute(
                    select(OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close)
                    .where(OhlcvCandle.instrument_id == nifty, OhlcvCandle.timeframe == timeframe)
                )).all()
                sources["candles"] = rows
            if index_ids:
                rows = (await self.db.execute(
                    select(BfOhlcvBar.ts, BfOhlcvBar.open, BfOhlcvBar.high, BfOhlcvBar.low, BfOhlcvBar.close)
                    .where(BfOhlcvBar.symbol_id.in_(index_ids), BfOhlcvBar.timeframe == timeframe)
                )).all()
                sources["backfill"] = rows
            merged: dict[date, dict[time, dict]] = {}
            for name, rows in sources.items():
                by_day: dict[date, dict[time, dict]] = defaultdict(dict)
                for ts, o, h, lo, c in rows:
                    moment = as_aware_utc(ts).astimezone(IST)
                    bar = to_bar(o, h, lo, c)
                    if bar is not None and SESSION_OPEN <= moment.time() < time(15, 30):
                        by_day[moment.date()][moment.time()] = bar
                if rows:
                    print(f"  {timeframe} ({name}): {len(rows)} bars, {len(by_day)} sessions, {min(by_day) if by_day else '-'} -> "
                          f"{max(by_day) if by_day else '-'}")
                for day, bars in by_day.items():  # the fuller of the two per session
                    if len(bars) > len(merged.get(day, {})):
                        merged[day] = bars
            self.spot[timeframe] = merged
        rows = (await self.db.execute(
            select(BfSymbol.id, BfSymbol.expiry, BfSymbol.strike, BfSymbol.option_type, BfSymbol.lot_size)
            .where(BfSymbol.underlying_symbol == "NIFTY", BfSymbol.option_type.is_not(None))
        )).all()
        for sid, expiry, strike, option_type, lot in rows:
            if expiry is not None and strike is not None:
                self.contracts[(expiry, float(strike), option_type)] = (sid, lot or DEFAULT_LOT)
        snaps = (await self.db.execute(
            select(PcrSnapshot.session_date, PcrSnapshot.expiries).where(PcrSnapshot.underlying == "NIFTY").order_by(PcrSnapshot.ts)
        )).all()
        for day, expiries in snaps:
            if day not in self.expiries and expiries:
                self.expiries[day] = [date.fromisoformat(x) for x in expiries]

    def weekly(self, day: date) -> date | None:
        """The nearest weekly listed that day -- next week's on its own expiry day."""
        listed = [e for e in self.expiries.get(day) or sorted({k[0] for k in self.contracts}) if e >= day]
        if not listed:
            return None
        return listed[1] if listed[0] == day and len(listed) > 1 else (None if listed[0] == day else listed[0])

    async def option_bars(self, key, day: date) -> dict[time, dict]:
        if (key, day) in self.bars:
            return self.bars[(key, day)]
        found = self.contracts.get(key)
        bars: dict[time, dict] = {}
        if found is not None:
            start = at(day, time(9, 0)).astimezone(timezone.utc)
            rows = (await self.db.execute(
                select(BfOhlcvBar.ts, BfOhlcvBar.open, BfOhlcvBar.high, BfOhlcvBar.low, BfOhlcvBar.close)
                .where(BfOhlcvBar.symbol_id == found[0], BfOhlcvBar.timeframe == "15m",
                       BfOhlcvBar.ts >= start, BfOhlcvBar.ts < start + timedelta(hours=7))
            )).all()
            for ts, o, h, lo, c in rows:
                bar = to_bar(o, h, lo, c)
                if bar is not None:
                    bars[as_aware_utc(ts).astimezone(IST).time()] = bar
            spot = self.spot["15m"].get(day, {})
            sign = 1.0 if key[2] == "CE" else -1.0
            for t, bar in bars.items():  # the estimate checked against each candle's own close
                s = spot.get(t)
                if s is not None:
                    move = s["close"] - s["open"]
                    estimate = min(max(bar["open"] + sign * DELTA * move, bar["low"]), bar["high"])
                    realised = sign * (bar["close"] - bar["open"]) / move if abs(move) >= 10 else None
                    self.estimates.append((abs(estimate - bar["close"]), abs(bar["close"] - bar["open"]), realised))
        self.bars[(key, day)] = bars
        return bars

    async def price(self, key, moment: datetime) -> float | None:
        """The option at `moment`: its 15-minute candle's open on the grid; inside it, the open + DELTA x NIFTY's move
        since (within the candle's high and low); a candle missing, the last one's close."""
        day = moment.date()
        bars = await self.option_bars(key, day)
        start = bar_start(moment)
        bar = bars.get(start.time())
        if bar is None:
            earlier = [t for t in bars if t < start.time()]
            return bars[max(earlier)]["close"] if earlier else None
        if moment == start:
            return bar["open"]
        spot_bar = self.spot["15m"].get(day, {}).get(start.time())
        now = self.spot_at(moment)
        if spot_bar is None or now is None:  # no NIFTY then: the candle's close near its end, else its open
            return bar["close"] if moment >= start + BAR - timedelta(minutes=1) else bar["open"]
        sign = 1.0 if key[2] == "CE" else -1.0
        return min(max(bar["open"] + sign * DELTA * (now - spot_bar["open"]), bar["low"]), bar["high"])

    def spot_at(self, moment: datetime) -> float | None:
        """NIFTY at `moment`: the close of the finest candle ending then, else the 15-minute candle's open on the grid."""
        for timeframe, length in FINE.items():
            bar = self.spot.get(timeframe, {}).get(moment.date(), {}).get((moment - length).time())
            if bar is not None:
                return bar["close"]
        bar = self.spot["15m"].get(moment.date(), {}).get(moment.time())
        return bar["open"] if bar is not None else None


def previous_candle(data: Data, moment: datetime, wait: timedelta) -> tuple[datetime, dict] | None:
    """The latest 15-minute NIFTY candle ended by `moment` (the session before's last before 09:30); None while a candle
    that has just ended is still `wait` from counting -- the live waits for it then, what's held kept."""
    day = moment.date()
    first_end = at(day, time(9, 30))
    if moment < first_end:
        before = previous_trading_day(day)
        bar = data.spot["15m"].get(before, {}).get(LAST_BAR)
        return (at(before, LAST_BAR), bar) if bar is not None else None
    end = min(first_end + int((moment - first_end) / BAR) * BAR, at(day, time(15, 30)))
    if moment - end < wait and end < at(day, time(15, 30)):
        return None
    start = end - BAR
    bar = data.spot["15m"].get(day, {}).get(start.time())
    return (start, bar) if bar is not None else None


def decision_times(data: Data, day: date, mode: str) -> list[tuple[datetime, float]]:
    """(when, NIFTY then) for one session."""
    if mode == "15m":
        bars = data.spot["15m"].get(day, {})
        return [(at(day, t), bars[t]["open"]) for t in sorted(bars) if t <= LAST_BAR]
    length = FINE[mode]
    bars = data.spot[mode].get(day, {})
    out = []
    first = bars.get(SESSION_OPEN)
    if first is not None:
        out.append((at(day, SESSION_OPEN), first["open"]))
    for t in sorted(bars):
        moment = at(day, t) + length
        if moment.time() < DAY_CLOSE:
            out.append((moment, bars[t]["close"]))
    return out


def level_stream(data: Data, day: date) -> list[tuple[datetime, float, dict | None]]:
    """OHLC4 Buy 5 MIN as deployed: (each 5-minute close 09:20-15:25, the candle's close, the candle before it) -- the
    09:15 candle against the session before's 15:25 one."""
    bars = data.spot["5m"].get(day, {})
    before = data.spot["5m"].get(previous_trading_day(day), {})
    out = []
    for t in sorted(bars):
        moment = at(day, t) + FINE["5m"]
        if moment.time() > time(15, 25):
            continue
        prior = before.get(time(15, 25)) if t == SESSION_OPEN else bars.get((at(day, t) - FINE["5m"]).time())
        out.append((moment, bars[t]["close"], prior))
    return out


async def simulate(data: Data, days: list[date], mode: str) -> dict:
    trades, pos, missed, waits = [], None, 0, 0

    def book(price: float, reason: str, moment: datetime) -> None:
        nonlocal pos
        sold = price * (1 - HALF_SPREAD)
        option_type, qty, entry = pos["key"][2], pos["qty"], pos["entry"]
        intraday = pos["opened"].date() == moment.date()
        cost = charges(option_type, qty, entry, True, intraday) + charges(option_type, qty, sold, False, intraday)
        gross = sold - entry
        trades.append({"day": moment.date(), "opened": pos["opened"], "closed": moment, "reason": reason, "side": option_type,
                       "gross": gross, "points": gross - cost / qty, "cost": cost / qty, "paid": entry})
        pos = None

    for n, day in enumerate(days):
        follows = n + 1 < len(days) and days[n + 1] == next_trading_day(day)
        stream = level_stream(data, day) if mode == "5m level" else [(m, s, None) for m, s in decision_times(data, day, mode)]
        for moment, spot, prior in stream:
            if pos is not None and pos["key"][0] <= day and moment.time() >= ROLL_AT:
                price = await data.price(pos["key"], moment)
                if price is not None:
                    book(price, "expiry_rollover", moment)
            candle = (moment, prior) if mode == "5m level" else previous_candle(data, moment, timedelta(0) if mode == "15m" else CANDLE_WAIT)
            if candle is None or candle[1] is None:
                waits += 1
                continue
            _start, bar = candle
            level = level_of(bar)
            side = "CE" if spot > level else ("PE" if spot < level else None)
            if pos is not None and side is not None and side != pos["key"][2]:
                price = await data.price(pos["key"], moment)
                if price is not None:
                    stopped = spot <= bar["low"] if pos["key"][2] == "CE" else spot >= bar["high"]
                    book(price, "stop_loss" if stopped else "level_flip", moment)
            if pos is None and side is not None:
                weekly = data.weekly(day)
                key = (weekly, atm(spot), side)
                price = await data.price(key, moment) if weekly is not None else None
                if price is None:
                    missed += 1
                    continue
                pos = {"key": key, "entry": price * (1 + HALF_SPREAD), "qty": float(data.contracts[key][1]), "opened": moment}
        if pos is not None:
            close = at(day, DAY_CLOSE)
            price = await data.price(pos["key"], close)
            price = price if price is not None else pos["entry"]
            if pos["key"][0] <= day:
                book(price, "expiry_day_close", close)
            elif not follows:
                book(price, "end_of_data" if n + 1 == len(days) else "gap_in_data", close)
    return {"trades": trades, "missed": missed, "waits": waits}


def summary(trades: list[dict], days: list[date]) -> dict:
    by_day = defaultdict(float)
    count_day = defaultdict(int)
    for t in trades:
        by_day[t["day"]] += t["points"]
        count_day[t["day"]] += 1
    equity = peak = drawdown = 0.0
    for d in days:
        equity += by_day.get(d, 0.0)
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    half = set(days[: len(days) // 2])
    holds = [(t["closed"] - t["opened"]).total_seconds() / 60 for t in trades]
    counts = [count_day.get(d, 0) for d in days]
    return {
        "trades": len(trades), "points": sum(t["points"] for t in trades), "gross": sum(t["gross"] for t in trades),
        "cost": sum(t["cost"] for t in trades), "win": 100 * sum(t["points"] > 0 for t in trades) / len(trades) if trades else 0.0,
        "flips": sum(t["reason"] == "level_flip" for t in trades), "stops": sum(t["reason"] == "stop_loss" for t in trades),
        "rolls": sum(t["reason"] == "expiry_rollover" for t in trades),
        "days_up": sum(by_day.get(d, 0.0) > 0 for d in days), "drawdown": drawdown,
        "best": max(by_day.values(), default=0.0), "worst": min(by_day.values(), default=0.0),
        "first": sum(v for d, v in by_day.items() if d in half), "second": sum(v for d, v in by_day.items() if d not in half),
        "hold_median": statistics.median(holds) if holds else 0.0,
        "per_day_median": statistics.median(counts) if counts else 0, "per_day_max": max(counts, default=0),
        "by_day": by_day, "count_day": count_day,
    }


async def main() -> None:
    async with AsyncSessionLocal() as db:
        data = Data(db)
        await data.load()
        span = (await db.execute(
            select(func.min(BfOhlcvBar.ts), func.max(BfOhlcvBar.ts)).join(BfSymbol, BfSymbol.id == BfOhlcvBar.symbol_id)
            .where(BfSymbol.underlying_symbol == "NIFTY", BfSymbol.option_type.is_not(None), BfOhlcvBar.timeframe == "15m")
        )).one()
        if span[0] is None:
            print("no NIFTY option candles stored: nothing to replay")
            return
        first_option, last_option = as_aware_utc(span[0]).astimezone(IST).date(), as_aware_utc(span[1]).astimezone(IST).date()
        now = datetime.now(timezone.utc).astimezone(IST)
        whole = sorted(d for d, bars in data.spot["15m"].items() if len(bars) >= BARS_A_DAY and first_option <= d <= last_option
                       and (d < now.date() or now.time() >= time(15, 30)))
        fine = next((tf for tf in FINE if sum(len(data.spot[tf].get(d, {})) >= 0.9 * (375 / (FINE[tf] / timedelta(minutes=1)))
                                               for d in whole) >= 0.8 * len(whole)), None) if whole else None
        print(f"option candles: {first_option} -> {last_option}; whole sessions with NIFTY's 15-minute candles there: {len(whole)}"
              + (f" ({whole[0]} -> {whole[-1]})" if whole else ""))
        if not whole:
            return
        modes = ["15m"]
        if fine is not None:
            days_fine = [d for d in whole if len(data.spot[fine].get(d, {})) >= 0.9 * (375 / (FINE[fine] / timedelta(minutes=1)))]
            print(f"finest NIFTY candles for 'every minute': {fine}, whole on {len(days_fine)} of the {len(whole)} sessions")
            modes.insert(0, fine)
        else:
            days_fine = []
            print("no 1-minute or 5-minute NIFTY candles over these sessions: only the 15-minute reading runs")
        days_5m = [d for d in whole if len(data.spot["5m"].get(d, {})) >= 0.9 * 75]
        if days_5m:
            modes.insert(0, "5m level")
        print()
        print("As deployed (OHLC4 Buy 5 MIN): at each 5-minute close 09:20-15:25 the candle's close against the candle before's")
        print("  (O+H+L+C)/4 -- the option at that close (estimated inside its 15-minute candle; the live trades 10-70 s later,")
        print("  when the candle is in); the rest as below. The other two readings: the 15-minute version backtested before")
        print("Rules: the previous 15-minute NIFTY candle's (O+H+L+C)/4 the level (09:15-09:30: the session before's last); NIFTY")
        print("  above it the ATM weekly call held, under it the put, flipped on a cross; 'stop_loss' when past the candle's low /")
        print("  high; no trailing stop; held overnight, rolled 12:00 on its expiry day to the side NIFTY is on; 1 lot; bought")
        print("  +0.1%, sold -0.1%, taxes and fees as live, no brokerage")
        results = {}
        for mode in modes:
            days = days_5m if mode == "5m level" else days_fine if mode != "15m" else whole
            try:
                run = await simulate(data, days, mode)
            except Exception as exc:  # noqa: BLE001 -- the kind and this script's line only, never the message
                lines = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename in ("<stdin>", __file__)]
                print(f"  {mode}: not run ({type(exc).__name__} at line {lines[-1] if lines else '?'})")
                continue
            results[mode] = (days, run, summary(run["trades"], days))
        if fine is not None and "15m" in results and set(days_fine) != set(whole):  # the 15-minute reading on the same sessions too
            try:
                run = await simulate(data, days_fine, "15m")
                results["15m same"] = (days_fine, run, summary(run["trades"], days_fine))
            except Exception as exc:  # noqa: BLE001
                print(f"  15m on the same sessions: not run ({type(exc).__name__})")
    report(data, results, fine)


def report(data: Data, results: dict, fine: str | None) -> None:
    names = {fine: "15m level, every " + str(fine), "15m": "15m level, 15-min closes", "15m same": "15-min closes, same sessions",
             "5m level": "5m level, 5-min closes"}
    print()
    print("1 lot, points per unit, net of the half-spread and charges:")
    print(f"  {'reading':<30} {'sessions':>8} {'net':>8} {'gross':>8} {'taxes':>7} {'1st half':>8} {'2nd half':>8} {'win%':>5} "
          f"{'trades':>6} {'flips':>5} {'stop':>4} {'rolls':>5} {'a day':>9} {'hold':>6} {'days up':>7} {'worst day':>9} {'drawdown':>8}")
    for mode, (days, run, s) in results.items():
        print(f"  {names.get(mode, mode):<30} {len(days):>8} {s['points']:>+8.1f} {s['gross']:>+8.1f} {s['cost']:>7.1f} "
              f"{s['first']:>+8.1f} {s['second']:>+8.1f} {s['win']:>5.1f} {s['trades']:>6} {s['flips']:>5} {s['stops']:>4} "
              f"{s['rolls']:>5} {s['per_day_median']:>4.0f}/{s['per_day_max']:<4} {s['hold_median']:>5.0f}m "
              f"{s['days_up']:>3}/{len(days):<3} {s['worst']:>+9.1f} {s['drawdown']:>+8.1f}")
        print(f"    buys passed over (no option price): {run['missed']}; decisions waiting for a new candle: {run['waits']}")
    print("  ('gross' = after the 0.1% half-spread, before taxes; 'a day' = trades a session, median / most; 'hold' = the median "
          "minutes held)")
    for mode, (days, run, s) in results.items():
        buckets = (("under 2 min", 0, 2), ("2-15 min", 2, 15), ("15-60 min", 15, 60), ("1 hour +", 60, 10 ** 9))
        parts = []
        for name, lo, hi in buckets:
            picked = [t for t in run["trades"] if lo <= (t["closed"] - t["opened"]).total_seconds() / 60 < hi]
            parts.append(f"{name} {len(picked)} ({sum(t['points'] for t in picked):+.1f})")
        print(f"  {names.get(mode, mode)} by time held: " + ", ".join(parts))
        sides = {side: [t for t in run["trades"] if t["side"] == side] for side in ("CE", "PE")}
        print(f"    calls {len(sides['CE'])} ({sum(t['points'] for t in sides['CE']):+.1f}), puts {len(sides['PE'])} "
              f"({sum(t['points'] for t in sides['PE']):+.1f}); median premium paid "
              f"{statistics.median([t['paid'] for t in run['trades']]) if run['trades'] else 0:.1f}")
    if data.estimates:
        errors = sorted(e for e, _m, _r in data.estimates)
        moves = sorted(m for _e, m, _r in data.estimates)
        realised = sorted(r for _e, _m, r in data.estimates if r is not None)
        print(f"the inside-a-candle estimate against {len(errors)} option candles' own closes: median miss {errors[len(errors) // 2]:.1f} "
              f"pts (the candles' median move {moves[len(moves) // 2]:.1f}); realised delta median "
              f"{realised[len(realised) // 2] if realised else float('nan'):.2f} (used {DELTA})")
    print("points per unit and trades by session:")
    order = list(results)
    print("  D,session," + ",".join(f"{names.get(m, m)} pts,trades" for m in order))
    for d in sorted({d for days, _r, _s in results.values() for d in days}):
        print(f"  D,{d:%d %b}," + ",".join(
            f"{results[m][2]['by_day'].get(d, 0.0):+.1f},{results[m][2]['count_day'].get(d, 0)}" if d in results[m][0] else "-,-"
            for m in order))


if __name__ == "__main__":
    asyncio.run(main())
