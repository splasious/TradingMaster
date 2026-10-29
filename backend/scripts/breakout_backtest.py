"""52 Candle Breakout 15 MIN -- backtest on 15, 30, 45 and 75-minute candles
(28 Oct), or on daily, weekly and monthly ones (29 Oct; RUN picks the set),
Rs 2 L a trade, at most 10 stocks held.

Read-only. Run inside the backend container (the health check's
`breakout_backtest` input pipes it to `python -`). The 30, 45 and 75-minute
candles are built from the stored 15-minute ones (resample: each session
from 09:15; at 30 and 45 minutes the session's last candle, 15:15-15:30, is
a short one, as Kite's own 30-minute candles are). On each timeframe, over
every candle of the strategy's 500 stocks, walked in time order -- "candle"
and both counts below in that timeframe's candles. Daily candles are the
stored ones; weekly (Monday-Friday) and monthly ones are built from them,
the period still running left out; on those "the session's last candle"
doesn't apply -- every one is bought at the next candle's open:

  In:  a candle closes above the highest high of the BREAKOUT_CANDLES (52)
       before it, while the stock isn't held -- bought at the next candle's
       open (the strategy buys at the live price about 4 minutes after the
       close). The session's last candle (ending 15:30) isn't bought. At
       most MAX_POSITIONS (10) stocks are held: a candle's breakouts fill
       the free slots strongest first (% over the 52-candle high), as the
       strategy ranks them; the rest are passed over.
  Out: a candle closed after the buy closes below the stock's own MA of
       (O+H+L+C)/4 over MA_CANDLES (10) -- sold at the next candle's open,
       the next session's first for the day's last candle. A candle's
       exits free their slots before its breakouts are ranked.
  Size: TRADE_SIZE (Rs 2,00,000) a trade, whole shares -- Rs 20 L in all
       at 10 held, the card's pool.

Charges are the app's own estimate (trade_record.charge_breakdown: delivery,
or intraday when bought and sold the same day). The results -- a summary,
an equity curve and every trade as a CSV -- go to the PRIVATE Telegram chat
only. This repository and its Actions log are public, so the log gets the
number of trades and the dates alone.
"""

import asyncio
import csv
import io
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from itertools import pairwise

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.time import as_aware_utc
from app.db.session import AsyncSessionLocal
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST
from app.services.notifications.telegram import send_telegram
from app.services.paper_trading.trade_record import charge_breakdown
from app.services.strategy.native_strategies import candle_breakout_52_15min as bo

TRADE_SIZE = 200_000.0
MAX_POSITIONS = bo.MAX_POSITIONS  # 10
TIMEFRAMES = (15, 30, 45, 75)  # minutes
PERIODS = ("Daily", "Weekly", "Monthly")
RUN = ("periods",)  # "intraday": the 15-75 minute set; "periods": daily, weekly, monthly
FIRST_START, LAST_START = time(9, 15), time(15, 15)  # the session's 25 15-minute candles
SESSION_END = time(15, 30)


def resample(bars: list[dict], minutes: int) -> list[dict]:
    """15-minute bars (oldest first) into `minutes` bars, each session's
    from 09:15: open of the first, highest high, lowest low, close of the
    last. New dicts, so each timeframe's run marks its own."""
    out: list[dict] = []
    for bar in bars:
        opening = datetime.combine(bar["start"].date(), FIRST_START, tzinfo=IST)
        start = opening + ((bar["start"] - opening) // timedelta(minutes=minutes)) * timedelta(minutes=minutes)
        if out and out[-1]["start"] == start:
            last = out[-1]
            last["high"], last["low"], last["close"] = max(last["high"], bar["high"]), min(last["low"], bar["low"]), bar["close"]
        else:
            out.append({"start": start, "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"]})
    return out


def to_period(days: list[dict], period: str, today) -> list[dict]:
    """Daily bars (oldest first) as Daily / Weekly / Monthly bars. "start"
    is the period's first calendar day (09:15 IST), the same for every
    stock, so a period's breakouts are ranked together; "opened" is its
    first trading day, when a buy or sell at its open happens; "closed" its
    last one's close (15:30). The period `today` is in -- not finished --
    left out."""
    def key(d):
        if period == "Weekly":
            return d - timedelta(days=d.weekday())
        if period == "Monthly":
            return d.replace(day=1)
        return d

    out: list[dict] = []
    for bar in days:
        start = datetime.combine(key(bar["start"].date()), FIRST_START, tzinfo=IST)
        closed = datetime.combine(bar["start"].date(), SESSION_END, tzinfo=IST)
        if out and out[-1]["start"] == start:
            last = out[-1]
            last["high"], last["low"], last["close"] = max(last["high"], bar["high"]), min(last["low"], bar["low"]), bar["close"]
            last["closed"] = closed
        else:
            out.append({"start": start, "opened": bar["start"], "closed": closed, "open": bar["open"], "high": bar["high"],
                        "low": bar["low"], "close": bar["close"]})
    if out and out[-1]["start"].date() == key(today):
        out.pop()
    return out


def last_of_session(start: datetime, minutes: int) -> bool:
    """The candle that ends with the session (at 15:30, or a short one cut there)."""
    return (start + timedelta(minutes=minutes)).time() >= SESSION_END or (start + timedelta(minutes=minutes)).date() > start.date()


def prepare(bars: list[dict], minutes: int | None = 15) -> list[dict]:
    """Marks each bar (oldest first, session candles only) with what the
    rules see at its close: "breakout" -- % over the 52-candle high, when
    it's one that can be bought at the next candle's open -- and
    "below_ma" -- closed under its 10-candle MA of (O+H+L+C)/4. `minutes`
    None: daily or longer bars, each bought at the next one's open."""
    typical = [bo.typical(b) for b in bars]
    for i, bar in enumerate(bars):
        bar["breakout"], bar["below_ma"] = None, False
        if i + 1 >= bo.MA_CANDLES:
            bar["below_ma"] = bar["close"] < sum(typical[i - bo.MA_CANDLES + 1:i + 1]) / bo.MA_CANDLES
        if i < bo.BREAKOUT_CANDLES or (minutes is not None and last_of_session(bar["start"], minutes)):
            continue
        prior_high = max(b["high"] for b in bars[i - bo.BREAKOUT_CANDLES:i])
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        same_session = nxt is not None and (minutes is None or nxt["start"].date() == bar["start"].date())
        if bar["close"] > prior_high > 0 and same_session and nxt["open"] > 0:
            bar["breakout"] = (bar["close"] / prior_high - 1) * 100
    return bars


def backtest(stocks: dict[str, list[dict]], max_positions: int = MAX_POSITIONS, minutes: int | None = 15) -> tuple[list[dict], int]:
    """(every trade, breakouts passed over for want of a slot). `stocks`:
    symbol -> its prepared bars. A trade still open at the end has exit_at
    None and is marked at the stock's last close."""
    timeline: dict[datetime, list[tuple[str, int]]] = defaultdict(list)
    for symbol, bars in stocks.items():
        for i, bar in enumerate(bars):
            timeline[bar["start"]].append((symbol, i))
    held: dict[str, dict] = {}
    trades: list[dict] = []
    passed = 0
    for start in sorted(timeline):
        breakouts = []
        for symbol, i in timeline[start]:
            bars = stocks[symbol]
            bar = bars[i]
            trade = held.get(symbol)
            if trade is not None:
                if i >= trade["entry_index"] and bar["below_ma"] and i + 1 < len(bars):
                    exit_bar = bars[i + 1]
                    trade.update(exit_at=exit_bar.get("opened", exit_bar["start"]), exit_price=exit_bar["open"],
                                 candles_held=i + 1 - trade["entry_index"])
                    trades.append(held.pop(symbol))
            elif bar["breakout"] is not None:
                breakouts.append((bar["breakout"], symbol, i))
        breakouts.sort(key=lambda b: (-b[0], b[1]))
        for pct, symbol, i in breakouts:
            if len(held) >= max_positions:
                passed += 1
                continue
            entry = stocks[symbol][i + 1]
            quantity = int(TRADE_SIZE / entry["open"])
            if quantity <= 0:
                continue
            held[symbol] = {
                "symbol": symbol, "breakout_at": stocks[symbol][i].get("closed") or stocks[symbol][i]["start"] + timedelta(minutes=minutes),
                "breakout_pct": pct,
                "entry_at": entry.get("opened", entry["start"]), "entry_price": entry["open"], "quantity": quantity, "entry_index": i + 1,
            }
    for symbol, trade in held.items():
        bars = stocks[symbol]
        trade.update(exit_at=None, exit_price=bars[-1]["close"], candles_held=len(bars) - trade["entry_index"])
        trades.append(trade)
    for t in trades:
        gross = (t["exit_price"] - t["entry_price"]) * t["quantity"]
        charges = None
        if t["exit_at"] is not None:
            leg = {"exchange": "NSE", "instrument_type": "equity", "side": "long", "quantity": t["quantity"],
                   "entry_price": t["entry_price"], "exit_price": t["exit_price"]}
            breakdown = charge_breakdown([leg], t["entry_at"], t["exit_at"])
            charges = breakdown["total"] if breakdown else None
        t.update(gross=gross, charges=charges, net=gross - (charges or 0.0), ret_pct=(t["exit_price"] / t["entry_price"] - 1) * 100)
    return trades, passed


def stats(trades: list[dict], passed: int, minutes: int | None) -> dict:
    closed = sorted((t for t in trades if t["exit_at"] is not None), key=lambda t: t["exit_at"])
    wins = [t for t in closed if t["net"] > 0]
    losses = [t for t in closed if t["net"] <= 0]
    peak = running = drawdown = 0.0
    for t in closed:
        running += t["net"]
        peak = max(peak, running)
        drawdown = min(drawdown, running - peak)
    events = sorted([(t["entry_at"], 1) for t in trades] + [(t["exit_at"], -1) for t in closed])
    held = most = 0
    for _, step in events:
        held += step
        most = max(most, held)
    candles = sorted(t["candles_held"] for t in closed)
    days = sorted((t["exit_at"] - t["entry_at"]).days for t in closed)
    loss_sum = -sum(t["net"] for t in losses)
    net = sum(t["net"] for t in closed)
    return {
        "closed": len(closed), "open": len(trades) - len(closed), "passed": passed,
        "win": len(wins) / len(closed) * 100 if closed else None,
        "avg": sum(t["ret_pct"] for t in closed) / len(closed) if closed else None,
        "avg_win": sum(t["ret_pct"] for t in wins) / len(wins) if wins else None,
        "avg_loss": sum(t["ret_pct"] for t in losses) / len(losses) if losses else None,
        "held_candles": candles[len(candles) // 2] if candles else None,
        "held_hours": candles[len(candles) // 2] * minutes / 60 if candles and minutes else None,
        "held_days": days[len(days) // 2] if days else None,
        "first": min((t["entry_at"] for t in trades), default=None),
        "gross": sum(t["gross"] for t in closed), "charges": sum(t["charges"] or 0.0 for t in closed), "net": net,
        "on_capital": net / (MAX_POSITIONS * TRADE_SIZE) * 100,
        "pf": sum(t["net"] for t in wins) / loss_sum if loss_sum > 0 else None,
        "drawdown": drawdown, "most": most,
        "months": {m: sum(t["net"] for t in closed if f"{t['exit_at'].astimezone(IST):%Y-%m}" == m)
                   for m in sorted({f"{t['exit_at'].astimezone(IST):%Y-%m}" for t in closed})},
    }


def label(timeframe) -> str:
    return f"{timeframe} min" if isinstance(timeframe, int) else timeframe


def compare(results: dict, stocks: int, first: datetime, last: datetime) -> str:
    """The timeframes side by side: one column each. `results`: minutes
    (int) or a period name -> (trades, passed)."""
    st = {m: stats(trades, passed, m if isinstance(m, int) else None) for m, (trades, passed) in results.items()}
    cols = list(st)
    intraday = any(isinstance(m, int) for m in cols)

    def row(label: str, key: str, fmt) -> str:
        return f"{label:<12}" + "".join(f"{'-' if st[m][key] is None else fmt(st[m][key]):>11}" for m in cols)

    lines = [
        f"Range  {first:%d %b %Y} - {last:%d %b %Y}",
        f"Stocks {stocks} with candles",
        f"Rs {TRADE_SIZE / 1e5:,.0f} L a trade, {MAX_POSITIONS} held at most (Rs {MAX_POSITIONS * TRADE_SIZE / 1e5:,.0f} L)",
        "",
        f"{'Candles':<12}" + "".join(f"{label(m):>11}" for m in cols),
        row("Trades", "closed", lambda v: f"{v:,}"),
        row("Open now", "open", lambda v: f"{v:,}"),
        row("Passed over", "passed", lambda v: f"{v:,}"),
        row("Win rate", "win", lambda v: f"{v:.1f}%"),
        row("Avg trade", "avg", lambda v: f"{v:+.2f}%"),
        row("Avg win", "avg_win", lambda v: f"{v:+.2f}%"),
        row("Avg loss", "avg_loss", lambda v: f"{v:+.2f}%"),
        row("Held, candles", "held_candles", lambda v: f"{v}"),
        row("Held, hours", "held_hours", lambda v: f"{v:.1f}") if intraday else row("Held, days", "held_days", lambda v: f"{v:,}"),
        row("First buy", "first", lambda v: f"{v.astimezone(IST):%b %Y}"),
        "",
        row("Gross P&L", "gross", lambda v: f"{v / 1e5:+,.2f} L"),
        row("Charges", "charges", lambda v: f"{-v / 1e5:+,.2f} L"),
        row("Net P&L", "net", lambda v: f"{v / 1e5:+,.2f} L"),
        row("On capital", "on_capital", lambda v: f"{v:+.1f}%"),
        row("Profit fact.", "pf", lambda v: f"{v:.2f}"),
        row("Max drawdown", "drawdown", lambda v: f"{v / 1e5:+,.2f} L"),
        row("Most held", "most", lambda v: f"{v}"),
        "",
    ]
    months = sorted({month for m in cols for month in st[m]["months"]})
    by_year = len(months) > 24  # years of daily data: a row a year, not a month
    lines.append(f"{'Net by year' if by_year else 'Net by month':<12}" + "".join(f"{label(m):>11}" for m in cols))
    for key in sorted({month[:4] for month in months}) if by_year else months:
        row_ = [sum(v for month, v in st[m]["months"].items() if month.startswith(key)) for m in cols]
        lines.append(f"{key:<12}" + "".join(f"{v / 1e5:>+9,.2f} L" for v in row_))
    lines.append("")
    lines.append("Held hours are trading time (candles x minutes); a stock" if intraday
                 else "Held days are calendar days, buy to sale (median); a stock")
    lines.append("still open at the end is valued at its last close and left")
    lines.append("out of every figure but 'Open now'.")
    return "\n".join(lines)


def equity_chart(results: dict) -> bytes | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    drawn = False
    for (minutes, (trades, _)), color in zip(results.items(), ("#2563eb", "#16a34a", "#d97706", "#dc2626")):
        closed = sorted((t for t in trades if t["exit_at"] is not None), key=lambda t: t["exit_at"])
        if not closed:
            continue
        total, curve = 0.0, []
        for t in closed:
            total += t["net"]
            curve.append(total / 1e5)
        ax.plot([t["exit_at"].astimezone(IST) for t in closed], curve, color=color, linewidth=1.5, label=label(minutes))
        drawn = True
    if not drawn:
        plt.close(fig)
        return None
    ax.axhline(0, color="#9ca3af", linewidth=0.8)
    ax.set_title("52 Candle Breakout - backtest, Rs 2 L a trade, 10 held at most: cumulative net P&L (Rs lakh)", fontsize=9)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)
    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def trades_csv(trades: list[dict]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["symbol", "breakout_close_ist", "breakout_pct", "entry_ist", "entry_price", "quantity", "exit_ist", "exit_price",
                "candles_held", "return_pct", "gross_pnl", "charges", "net_pnl"])
    for t in sorted(trades, key=lambda t: t["entry_at"]):
        w.writerow([
            t["symbol"], f"{t['breakout_at'].astimezone(IST):%Y-%m-%d %H:%M}", f"{t['breakout_pct']:.2f}",
            f"{t['entry_at'].astimezone(IST):%Y-%m-%d %H:%M}", f"{t['entry_price']:.2f}", t["quantity"],
            f"{t['exit_at'].astimezone(IST):%Y-%m-%d %H:%M}" if t["exit_at"] else "open", f"{t['exit_price']:.2f}",
            t["candles_held"], f"{t['ret_pct']:.2f}", f"{t['gross']:.2f}", "" if t["charges"] is None else f"{t['charges']:.2f}",
            f"{t['net']:.2f}",
        ])
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


async def load_daily(db, instrument_id, today) -> list[dict]:
    """The stock's stored daily candles before `today`, oldest first."""
    rows = (
        await db.execute(
            select(OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close)
            .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == "1d")
            .order_by(OhlcvCandle.ts)
        )
    ).all()
    bars, seen = [], set()
    for ts, o, h, lo, c in rows:
        day = as_aware_utc(ts).astimezone(IST).date()
        if day < today and day not in seen and None not in (o, h, lo, c):
            seen.add(day)
            bars.append({"start": datetime.combine(day, FIRST_START, tzinfo=IST), "open": float(o), "high": float(h),
                         "low": float(lo), "close": float(c)})
    return bars


async def load_bars(db, instrument_id) -> list[dict]:
    rows = (
        await db.execute(
            select(OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close)
            .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == bo.TIMEFRAME)
            .order_by(OhlcvCandle.ts)
        )
    ).all()
    bars = []
    for ts, o, h, lo, c in rows:
        start = as_aware_utc(ts).astimezone(IST)
        if FIRST_START <= start.time() <= LAST_START and None not in (o, h, lo, c):
            bars.append({"start": start, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)})
    return bars


async def send_results(results: dict, stocks: int, first: datetime, last: datetime, title: str) -> int:
    await send_telegram(title, compare(results, stocks, first, last), photo=equity_chart(results))
    sent = 0
    for timeframe, (trades, _) in results.items():
        name = f"{timeframe}min" if isinstance(timeframe, int) else timeframe.lower()
        sent += await send_csv(trades_csv(trades), f"breakout52_{name}_backtest_{datetime.now(timezone.utc):%Y%m%d}.csv",
                               f"52 Candle Breakout backtest on {label(timeframe).lower()} candles: every trade")
    return sent


def gaps_down(bars_by_stock: dict[str, list[dict]], under: float = 0.65) -> int:
    """Days opening under `under` x the day before's close: a split or bonus
    the stored candles may not be adjusted for (a false MA exit)."""
    return sum(1 for bars in bars_by_stock.values() for a, b in pairwise(bars) if b["open"] < under * a["close"])


def _span(bars_by_stock: dict[str, list[dict]]) -> tuple[datetime | None, datetime | None]:
    starts = [b[0]["start"] for b in bars_by_stock.values() if b]
    ends = [b[-1]["start"] for b in bars_by_stock.values() if b]
    return (min(starts), max(ends)) if starts else (None, None)


async def main() -> None:
    today = datetime.now(timezone.utc).astimezone(IST).date()
    intraday: dict[str, list[dict]] = {}
    daily: dict[str, list[dict]] = {}
    async with AsyncSessionLocal() as db:
        universe = (
            await db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(bo.STOCK_UNIVERSE)))
        ).scalars().all()
        for instrument in universe:
            if "intraday" in RUN:
                bars = await load_bars(db, instrument.id)
                if len(bars) > bo.BREAKOUT_CANDLES:
                    intraday[instrument.symbol] = bars
            if "periods" in RUN:
                days = await load_daily(db, instrument.id, today)
                if days:
                    daily[instrument.symbol] = days
    report = []
    for which, by_stock, timeframes in (("intraday", intraday, TIMEFRAMES), ("periods", daily, PERIODS)):
        if which not in RUN:
            continue
        first, last = _span(by_stock)
        if first is None:
            report.append(f"{which}: no candles")
            continue
        results = {}
        for timeframe in timeframes:
            if isinstance(timeframe, int):
                prepared = {s: prepare(resample(b, timeframe), timeframe) for s, b in by_stock.items()}
            else:
                prepared = {s: prepare(to_period(b, timeframe, today), None) for s, b in by_stock.items()}
            results[timeframe] = backtest({s: b for s, b in prepared.items() if len(b) > bo.BREAKOUT_CANDLES},
                                          minutes=timeframe if isinstance(timeframe, int) else None)
        names = " / ".join(label(t) for t in timeframes)
        sent = await send_results(results, len(by_stock), first, last,
                                  f"52 Candle Breakout · backtest on {names} candles (Rs 2 L each, 10 held at most)")
        counts = ", ".join(f"{label(t)} {sum(1 for x in tr if x['exit_at'] is not None)} closed" for t, (tr, _) in results.items())
        gaps = f", {gaps_down(by_stock)} daily opens 35%+ under the close before" if which == "periods" else ""
        report.append(f"{counts}; {len(by_stock)} stocks, {first:%d %b %Y} - {last:%d %b %Y}{gaps}, CSV {sent}/{len(results)} sent")
    print("breakout backtest: sent to the private chat -- " + " | ".join(report))


if __name__ == "__main__":
    asyncio.run(main())
