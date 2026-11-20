"""Conti Calendar 15 MIN: one day's trades, and why that day went well (20 Nov).

Read-only. For DAY (below) and, to compare, every session the card has
traded:

- the strategy's versions and when each came in (number, md5, time), so
  each trade shows the rules it ran under;
- each session: trades, take-profits, winners (counts), other exits, the
  weekly / monthly it traded, NIFTY's path (09:30, 15:15, high-low), and
  how the weekly and monthly ATM straddles moved (points, %, a rough IV);
- DAY's trades one by one: times, minutes held, the wait after the last
  close, strike against NIFTY, exit reason, version, each leg's price in
  and out, and DAY's 15-minute records (NIFTY, PCR, change in OI, both
  straddles).

The public log gets times, strikes, counts, exit reasons, versions and
NIFTY market data (option prices included) -- no quantities, no rupees.
Each trade's and each session's rupees (net after charges) go to the
private Telegram chat only. Run inside the backend container (the health
check's `cc_day_review` input pipes it to `python -`).
"""

import asyncio
import hashlib
import math
import traceback
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import func, select

from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.paper_trading import PaperNativeDeployment, PaperNativeTrade
from app.models.pcr import PcrSnapshot, PcrStrikeOi
from app.models.strategy import Strategy, StrategyVersion
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram

NAME = "Conti Calendar 15 MIN"
DAY = date(2026, 11, 6)
UNDERLYING = "NIFTY"
STEP = 50.0
OPEN_MARK, LATE_MARK = dtime(9, 30), dtime(15, 15)
EXPIRY_CLOSE = dtime(15, 30)


def ist(dt: datetime) -> datetime:
    return as_aware_utc(dt).astimezone(IST)


def md5(code: str | None) -> str:
    return hashlib.md5((code or "").replace("\r", "").encode()).hexdigest()


def floor_mark(t: datetime) -> dtime:
    t = ist(t)
    return dtime(t.hour, t.minute - t.minute % 15)


def is_short(leg: dict) -> bool:
    return leg.get("side") in ("sell", "short")


def net_of(trade) -> float:
    return trade.pnl - (trade.charges or 0.0)


def failed(where: str, exc: BaseException) -> None:
    """The kind and this script's line only -- never the message text."""
    lines = [f.lineno for f in traceback.extract_tb(exc.__traceback__) if f.filename in ("<stdin>", __file__)]
    print(f"  {where}: not read ({type(exc).__name__} at line {lines[-1] if lines else '?'})")


def rough_iv(straddle: float | None, spot: float | None, expiry: date, at: datetime) -> float | None:
    """ATM straddle ~ 0.8 x spot x IV x sqrt(T): a rough implied volatility, in %."""
    if not straddle or not spot:
        return None
    years = (datetime.combine(expiry, EXPIRY_CLOSE, IST) - at).total_seconds() / (365 * 86400)
    return None if years <= 0 else 100 * straddle / (0.8 * spot * math.sqrt(years))


class Candles:
    """15-minute NIFTY option candles the server keeps: the fallback price
    where a change-in-OI record has no last price for a contract."""

    def __init__(self, db):
        self.db, self.ids, self.bars = db, {}, {}

    async def load(self) -> None:
        rows = (await self.db.execute(
            select(BfSymbol.id, BfSymbol.expiry, BfSymbol.strike, BfSymbol.option_type)
            .where(BfSymbol.underlying_symbol == UNDERLYING, BfSymbol.option_type.is_not(None))
        )).all()
        self.ids = {(e, float(k), t): i for i, e, k, t in rows if e and k is not None}

    async def close_before(self, day: date, key: tuple, mark: dtime) -> float | None:
        """The close of the 15-minute candle that ends at `mark`."""
        sid = self.ids.get(key)
        if sid is None:
            return None
        if (sid, day) not in self.bars:
            start = datetime.combine(day, dtime(9, 0), IST).astimezone(timezone.utc)
            bars = (await self.db.execute(
                select(BfOhlcvBar.ts, BfOhlcvBar.close).where(
                    BfOhlcvBar.symbol_id == sid, BfOhlcvBar.timeframe == "15m",
                    BfOhlcvBar.ts >= start, BfOhlcvBar.ts < start + timedelta(hours=7))
            )).all()
            self.bars[(sid, day)] = {ist(ts).time(): c for ts, c in bars}
        begin = (datetime.combine(day, mark) - timedelta(minutes=15)).time()
        return self.bars[(sid, day)].get(begin)


class Session:
    def __init__(self, day: date, snaps: list, prices: dict, candles: Candles):
        self.day, self.candles, self.prices = day, candles, prices
        self.snaps = {ist(s.ts).time(): s for s in snaps}

    def at(self, mark: dtime):
        return self.snaps.get(mark)

    def last_at_or_before(self, mark: dtime):
        marks = [m for m in self.snaps if m <= mark]
        return self.snaps[max(marks)] if marks else None

    async def price(self, expiry: date, strike: float, option_type: str, mark: dtime) -> float | None:
        snap = self.snaps.get(mark)
        found = self.prices.get(snap.id, {}).get((expiry, float(strike), option_type)) if snap else None
        return found if found is not None else await self.candles.close_before(self.day, (expiry, float(strike), option_type), mark)

    async def straddle(self, expiry: date, strike: float, mark: dtime) -> float | None:
        ce = await self.price(expiry, strike, "CE", mark)
        pe = await self.price(expiry, strike, "PE", mark)
        return ce + pe if ce is not None and pe is not None else None


async def load_session(db, day: date, candles: Candles) -> Session | None:
    snaps = (await db.execute(
        select(PcrSnapshot).where(PcrSnapshot.underlying == UNDERLYING, PcrSnapshot.session_date == day).order_by(PcrSnapshot.ts)
    )).scalars().all()
    snaps = [s for s in snaps if dtime(9, 15) <= ist(s.ts).time() <= dtime(15, 30)]
    spots = [s.spot for s in snaps if s.spot]
    if not spots:
        return None
    rows = (await db.execute(
        select(PcrStrikeOi.snapshot_id, PcrStrikeOi.expiry, PcrStrikeOi.strike, PcrStrikeOi.option_type, PcrStrikeOi.last_price)
        .where(PcrStrikeOi.snapshot_id.in_([s.id for s in snaps]),
               PcrStrikeOi.strike.between(min(spots) - 700, max(spots) + 700))
    )).all()
    prices: dict = defaultdict(dict)
    for sid, e, k, t, p in rows:
        if p is not None:
            prices[sid][(e, float(k), t)] = p
    return Session(day, snaps, prices, candles)


def atm(spot: float) -> float:
    return round(spot / STEP) * STEP


def fmt(x: float | None, spec: str = ",.2f") -> str:
    return "-" if x is None else format(x, spec)


def lakh(x: float | None) -> str:
    return "-" if x is None else f"{x / 1e5:+,.1f}L"


async def straddle_move(s: Session, expiry: date, a: dtime, b: dtime) -> dict:
    """The ATM straddle at `a` and `b` (each at that time's ATM), the one at
    `a`'s strike held to `b`, and a rough IV at both."""
    sa, sb = s.at(a), s.last_at_or_before(b)
    if sa is None or sb is None or not sa.spot or not sb.spot:
        return {}
    ka, kb = atm(sa.spot), atm(sb.spot)
    at_a = await s.straddle(expiry, ka, a)
    at_b = await s.straddle(expiry, kb, ist(sb.ts).time())
    held = await s.straddle(expiry, ka, ist(sb.ts).time())
    return {
        "k": ka, "a": at_a, "b": at_b, "held": held,
        "iv_a": rough_iv(at_a, sa.spot, expiry, ist(sa.ts)), "iv_b": rough_iv(at_b, sb.spot, expiry, ist(sb.ts)),
    }


def monthly_after(weekly: date, expiries: list[date]) -> date | None:
    last_of_month = {}
    for e in expiries:
        last_of_month[(e.year, e.month)] = max(e, last_of_month.get((e.year, e.month), e))
    return next((e for e in sorted(expiries) if e > weekly and last_of_month[(e.year, e.month)] == e), None)


async def main() -> None:
    private: list[str] = []
    async with AsyncSessionLocal() as db:
        strategy = (await db.execute(select(Strategy).where(func.trim(Strategy.name) == NAME))).scalars().first()
        versions = (await db.execute(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy.id).order_by(StrategyVersion.version_number)
        )).scalars().all()
        print(f"{NAME}: versions and when each came in (IST)")
        for v in versions:
            print(f"  v{v.version_number} {md5(v.python_code)[:8]} from {ist(v.created_at):%d %b %H:%M:%S}")

        def version_at(t: datetime) -> str:
            live = [v for v in versions if as_aware_utc(v.created_at) <= as_aware_utc(t)]
            return f"v{live[-1].version_number}" if live else "-"

        cards = (await db.execute(select(PaperNativeDeployment.id).where(PaperNativeDeployment.strategy_id == strategy.id))).scalars().all()
        trades = (await db.execute(
            select(PaperNativeTrade).where(PaperNativeTrade.deployment_id.in_(cards)).order_by(PaperNativeTrade.opened_at)
        )).scalars().all()
        print(f"  cards: {len(cards)}; closed trades: {len(trades)}")

        candles = Candles(db)
        await candles.load()
        expiries = sorted({k[0] for k in candles.ids})

        by_day: dict[date, list] = defaultdict(list)
        for t in trades:
            by_day[ist(t.closed_at).date()].append(t)
        days = sorted(by_day)

        print()
        print("Each session it closed trades (counts; NIFTY 09:30 -> 15:15 and the day's high-low from the 15-minute records;")
        print("  W = the weekly it sold, M = the monthly it bought: ATM straddle 09:30 -> 15:15, rough IV, and the 09:30 strike's straddle held to 15:15)")
        tp_rank = sorted(days, key=lambda d: -sum(1 for t in by_day[d] if t.exit_reason == "take_profit"))
        for d in days:
            ts = by_day[d]
            reasons = Counter(t.exit_reason for t in ts)
            tp = reasons.pop("take_profit", 0)
            wins = sum(1 for t in ts if net_of(t) > 0)
            shorts = [leg for leg in ts[0].legs if is_short(leg) and leg.get("expiry")]
            longs = [leg for leg in ts[0].legs if not is_short(leg) and leg.get("expiry")]
            weekly = date.fromisoformat(str(shorts[0]["expiry"])) if shorts else None
            monthly = date.fromisoformat(str(longs[0]["expiry"])) if longs else None
            held = [(t.closed_at - t.opened_at).total_seconds() / 60 for t in ts]
            line = (f"  {d:%d %b %a}: {len(ts)} trades, {tp} take-profits, {wins} winners"
                    f"{'; other exits ' + ', '.join(f'{r} x{n}' for r, n in reasons.items()) if reasons else ''}; "
                    f"held median {sorted(held)[len(held) // 2]:.0f} min")
            print(line)
            private.append(f"{d:%d %b %a}: {len(ts)} trades, {wins} winners, net Rs {sum(net_of(t) for t in ts):+,.0f} "
                           f"(gross {sum(t.pnl for t in ts):+,.0f}, charges {sum(t.charges or 0 for t in ts):,.0f})")
            try:
                s = await load_session(db, d, candles)
                if s is None:
                    print("    no change-in-OI records that day")
                    continue
                spots = [x.spot for x in s.snaps.values() if x.spot]
                o, c = s.at(OPEN_MARK), s.last_at_or_before(LATE_MARK)
                moves = [abs(x.spot_change) for m, x in s.snaps.items() if x.spot_change is not None and m > dtime(9, 15)]
                print(f"    NIFTY {fmt(o.spot if o else None, ',.0f')} -> {fmt(c.spot if c else None, ',.0f')}; high-low {max(spots) - min(spots):,.0f} "
                      f"({100 * (max(spots) - min(spots)) / min(spots):.2f}%); biggest 15-min move {fmt(max(moves) if moves else None, ',.0f')}; "
                      f"PCR {fmt(o.pcr if o else None, '.3f')} -> {fmt(c.pcr if c else None, '.3f')}")
                for label, e in (("W", weekly), ("M", monthly)):
                    if e is None:
                        continue
                    mv = await straddle_move(s, e, OPEN_MARK, LATE_MARK)
                    if not mv:
                        print(f"    {label} {e:%d %b}: no prices")
                        continue
                    drop = (mv["a"] - mv["held"]) if mv["a"] and mv["held"] else None
                    print(f"    {label} {e:%d %b} ({(e - d).days} days): ATM {fmt(mv['a'])} -> {fmt(mv['b'])}; IV ~{fmt(mv['iv_a'], '.1f')}% -> "
                          f"~{fmt(mv['iv_b'], '.1f')}%; {mv['k']:,.0f} held {fmt(mv['a'])} -> {fmt(mv['held'])} "
                          f"({fmt(-drop if drop is not None else None, '+,.2f')}, {fmt(-100 * drop / mv['a'] if drop is not None else None, '+.1f')}%)")
            except Exception as exc:  # noqa: BLE001
                failed("market data", exc)
        print(f"  most take-profits in a session: {', '.join(f'{d:%d %b}' for d in tp_rank[:3])}")
        ranked = sorted(days, key=lambda d: -sum(net_of(t) for t in by_day[d]))
        private.append(f"Rank by net: {', '.join(f'{d:%d %b}' for d in ranked)}")

        print()
        print(f"{DAY:%d %b %a}: each trade (times IST; spot at the 15-minute record before; W = weekly sold, M = monthly bought, per unit)")
        day_trades = [t for t in trades if ist(t.opened_at).date() == DAY or ist(t.closed_at).date() == DAY]
        previous_close = None
        for n, t in enumerate(day_trades, 1):
            try:
                legs = t.legs
                strikes = sorted({leg.get("strike") for leg in legs if leg.get("strike") is not None})
                sold = [leg for leg in legs if is_short(leg)]
                bought = [leg for leg in legs if not is_short(leg)]
                w_in, w_out = sum(leg["entry_price"] for leg in sold), sum(leg["exit_price"] for leg in sold)
                m_in, m_out = sum(leg["entry_price"] for leg in bought), sum(leg["exit_price"] for leg in bought)
                s = await load_session(db, ist(t.opened_at).date(), candles)
                so = s.last_at_or_before(floor_mark(t.opened_at)) if s else None
                sc = s.last_at_or_before(floor_mark(t.closed_at)) if s and ist(t.closed_at).date() == s.day else None
                gap = f"{(t.opened_at - previous_close).total_seconds():,.0f}s after the last close" if previous_close else "first"
                held = (t.closed_at - t.opened_at).total_seconds() / 60
                print(f"  #{n} {ist(t.opened_at):%H:%M:%S} -> {ist(t.closed_at):%d %b %H:%M:%S} ({held:.0f} min; {gap}); {version_at(t.opened_at)}; "
                      f"{t.exit_reason}; strike {', '.join(f'{k:,.0f}' for k in strikes)}")
                if so:
                    print(f"     NIFTY {so.spot:,.0f} at {ist(so.ts):%H:%M} (strike {strikes[0] - so.spot:+,.0f}; PCR {fmt(so.pcr, '.3f')})"
                          + (f" -> {sc.spot:,.0f} at {ist(sc.ts):%H:%M}" if sc and sc.spot else ""))
                print(f"     W sold {w_in:,.2f} -> {w_out:,.2f} ({w_out - w_in:+,.2f}, {100 * (w_out - w_in) / w_in:+.1f}%); "
                      f"M bought {m_in:,.2f} -> {m_out:,.2f} ({m_out - m_in:+,.2f}, {100 * (m_out - m_in) / m_in:+.1f}%); "
                      f"spread {(w_in - w_out) + (m_out - m_in):+,.2f} points")
                print("     legs: " + "; ".join(
                    f"{'sell' if is_short(leg) else 'buy'} {leg.get('expiry')} {leg.get('strike', 0):,.0f} {leg.get('option_type')} "
                    f"{leg['entry_price']:,.2f}->{leg['exit_price']:,.2f}" for leg in legs))
                private.append(f"{DAY:%d %b} #{n} {ist(t.opened_at):%H:%M}-{ist(t.closed_at):%H:%M} {t.exit_reason}: net Rs {net_of(t):+,.0f} "
                               f"(gross {t.pnl:+,.0f}, charges {t.charges or 0:,.0f})")
            except Exception as exc:  # noqa: BLE001
                failed(f"trade #{n}", exc)
            previous_close = t.closed_at

        print()
        print(f"{DAY:%d %b}: the 15-minute records (NIFTY, its move, PCR, change in OI in the last 15 min put / call,")
        print("  then the ATM straddle and rough IV of the weekly and monthly it traded)")
        try:
            s = await load_session(db, DAY, candles)
            first = next((t for t in day_trades if ist(t.opened_at).date() == DAY), None)
            weekly = date.fromisoformat(str(next(leg for leg in first.legs if is_short(leg))["expiry"])) if first else None
            monthly = date.fromisoformat(str(next(leg for leg in first.legs if not is_short(leg))["expiry"])) if first else None
            if weekly is None:
                weekly = next(e for e in expiries if e > DAY)
                monthly = monthly_after(weekly, expiries)
            print(f"  weekly {weekly} ({(weekly - DAY).days} days away), monthly {monthly} ({(monthly - DAY).days} days away)")
            for mark in sorted(s.snaps):
                x = s.snaps[mark]
                k = atm(x.spot) if x.spot else None
                w = await s.straddle(weekly, k, mark) if k else None
                m = await s.straddle(monthly, k, mark) if k and monthly else None
                print(f"  {mark:%H:%M} NIFTY {fmt(x.spot, ',.0f')} ({fmt(x.spot_change, '+,.0f')}), PCR {fmt(x.pcr, '.3f')}, "
                      f"OI {lakh(x.put_oi_change)} / {lakh(x.call_oi_change)}; ATM {fmt(k, ',.0f')}: W {fmt(w)} (IV ~{fmt(rough_iv(w, x.spot, weekly, ist(x.ts)), '.1f')}%), "
                      f"M {fmt(m)} (IV ~{fmt(rough_iv(m, x.spot, monthly, ist(x.ts)), '.1f')}%)"
                      + (f", M/W {m / w:.2f}" if w and m else ""))
        except Exception as exc:  # noqa: BLE001
            failed("the day's records", exc)

    await send_telegram(f"{NAME}: {DAY:%d %b} review (private)",
                        "Net after charges, each session it closed trades, then each trade that day:\n\n" + "\n".join(private))
    print()
    print("sent to the private Telegram chat: rupees per session and per trade, yes")


if __name__ == "__main__":
    asyncio.run(main())
