"""52 Candle Breakout 15 MIN -- backtest, Rs 2 L a trade, at most 10 stocks held (28 Oct).

Read-only. Run inside the backend container (the health check's
`breakout_backtest` input pipes it to `python -`). Over every 15-minute
candle stored for the strategy's 500 stocks, walked in time order:

  In:  a candle closes above the highest high of the BREAKOUT_CANDLES (52)
       before it, while the stock isn't held -- bought at the next candle's
       open (the strategy buys at the live price about 4 minutes after the
       close). The session's last candle (15:15-15:30) isn't bought. At
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
BAR = timedelta(minutes=15)
FIRST_START, LAST_START = time(9, 15), time(15, 15)  # the session's 25 candles


def prepare(bars: list[dict]) -> list[dict]:
    """Marks each bar (oldest first, session candles only) with what the
    rules see at its close: "breakout" -- % over the 52-candle high, when
    it's one that can be bought at the next candle's open -- and
    "below_ma" -- closed under its 10-candle MA of (O+H+L+C)/4."""
    typical = [bo.typical(b) for b in bars]
    for i, bar in enumerate(bars):
        bar["breakout"], bar["below_ma"] = None, False
        if i + 1 >= bo.MA_CANDLES:
            bar["below_ma"] = bar["close"] < sum(typical[i - bo.MA_CANDLES + 1:i + 1]) / bo.MA_CANDLES
        if i < bo.BREAKOUT_CANDLES or bar["start"].time() >= LAST_START:
            continue
        prior_high = max(b["high"] for b in bars[i - bo.BREAKOUT_CANDLES:i])
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        if bar["close"] > prior_high > 0 and nxt is not None and nxt["start"].date() == bar["start"].date() and nxt["open"] > 0:
            bar["breakout"] = (bar["close"] / prior_high - 1) * 100
    return bars


def backtest(stocks: dict[str, list[dict]], max_positions: int = MAX_POSITIONS) -> tuple[list[dict], int]:
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
                    trade.update(exit_at=exit_bar["start"], exit_price=exit_bar["open"], candles_held=i + 1 - trade["entry_index"])
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
                "symbol": symbol, "breakout_at": stocks[symbol][i]["start"] + BAR, "breakout_pct": pct,
                "entry_at": entry["start"], "entry_price": entry["open"], "quantity": quantity, "entry_index": i + 1,
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


def summarize(trades: list[dict], passed: int, stocks: int, first: datetime, last: datetime) -> str:
    closed = sorted((t for t in trades if t["exit_at"] is not None), key=lambda t: t["exit_at"])
    still_open = [t for t in trades if t["exit_at"] is None]
    if not closed:
        return f"No closed trades in {first:%d %b %Y} - {last:%d %b %Y} ({stocks} stocks)."
    wins = [t for t in closed if t["net"] > 0]
    losses = [t for t in closed if t["net"] <= 0]
    gross, charges, net = (sum(t[k] or 0.0 for t in closed) for k in ("gross", "charges", "net"))
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
    loss_sum = -sum(t["net"] for t in losses)
    lines = [
        f"Range     {first:%d %b %Y} - {last:%d %b %Y}",
        f"Stocks    {stocks} with candles",
        f"Size      Rs {TRADE_SIZE / 1e5:,.0f} L a trade, {MAX_POSITIONS} held at most",
        f"Capital   Rs {MAX_POSITIONS * TRADE_SIZE / 1e5:,.0f} L",
        "",
        f"Trades    {len(closed):,} closed, {len(still_open)} open now",
        f"Passed    {passed:,} breakouts, no slot free",
        f"Win rate  {len(wins) / len(closed) * 100:.1f}%",
        f"Avg trade {sum(t['ret_pct'] for t in closed) / len(closed):+.2f}%",
        f"Avg win   {sum(t['ret_pct'] for t in wins) / len(wins):+.2f}%" if wins else "Avg win   -",
        f"Avg loss  {sum(t['ret_pct'] for t in losses) / len(losses):+.2f}%" if losses else "Avg loss  -",
        f"Held      median {candles[len(candles) // 2]} candles",
        "",
        f"Gross P&L {gross:>+15,.0f}",
        f"Charges   {-charges:>+15,.0f}",
        f"Net P&L   {net:>+15,.0f}",
        f"On capital {net / (MAX_POSITIONS * TRADE_SIZE) * 100:+.1f}%",
        f"Profit factor {sum(t['net'] for t in wins) / loss_sum:.2f}" if loss_sum > 0 else "Profit factor -",
        f"Max drawdown {drawdown:>+12,.0f}",
        f"Most held at once {most} (Rs {most * TRADE_SIZE / 1e5:,.0f} L)",
        "",
        "Month     trades  win%      net P&L",
    ]
    by_month: dict[str, list[dict]] = defaultdict(list)
    for t in closed:
        by_month[f"{t['exit_at'].astimezone(IST):%Y-%m}"].append(t)
    for month, ts in sorted(by_month.items()):
        win = sum(1 for t in ts if t["net"] > 0) / len(ts) * 100
        lines.append(f"{month}  {len(ts):>6}  {win:>4.0f}  {sum(t['net'] for t in ts):>+12,.0f}")
    best = sorted(closed, key=lambda t: -t["ret_pct"])[:5]
    worst = sorted(closed, key=lambda t: t["ret_pct"])[:5]
    lines += ["", "Best"] + [f"  {t['symbol']:<12} {t['ret_pct']:+6.1f}%  {t['entry_at'].astimezone(IST):%d %b %y}" for t in best]
    lines += ["Worst"] + [f"  {t['symbol']:<12} {t['ret_pct']:+6.1f}%  {t['entry_at'].astimezone(IST):%d %b %y}" for t in worst]
    return "\n".join(lines)


def equity_chart(trades: list[dict]) -> bytes | None:
    closed = sorted((t for t in trades if t["exit_at"] is not None), key=lambda t: t["exit_at"])
    if not closed:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times, total, curve = [], 0.0, []
    for t in closed:
        total += t["net"]
        times.append(t["exit_at"].astimezone(IST))
        curve.append(total / 1e5)
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    ax.plot(times, curve, color="#2563eb", linewidth=1.6)
    ax.axhline(0, color="#9ca3af", linewidth=0.8)
    ax.set_title("52 Candle Breakout 15 MIN - backtest, Rs 2 L a trade, 10 held at most: cumulative net P&L (Rs lakh)", fontsize=9)
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


async def main() -> None:
    prepared: dict[str, list[dict]] = {}
    first, last = None, None
    async with AsyncSessionLocal() as db:
        universe = (
            await db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(bo.STOCK_UNIVERSE)))
        ).scalars().all()
        for instrument in universe:
            bars = await load_bars(db, instrument.id)
            if len(bars) <= bo.BREAKOUT_CANDLES:
                continue
            first = min(first or bars[0]["start"], bars[0]["start"])
            last = max(last or bars[-1]["start"], bars[-1]["start"])
            prepared[instrument.symbol] = prepare(bars)
    stocks = len(prepared)
    trades, passed = backtest(prepared)
    if not trades or first is None:
        print(f"breakout backtest: no trades ({stocks} stocks with candles)")
        return
    summary = summarize(trades, passed, stocks, first, last)
    await send_telegram("52 Candle Breakout 15 MIN · backtest (Rs 2 L each, 10 held at most)", summary, photo=equity_chart(trades))
    sent = await send_csv(trades_csv(trades), f"breakout52_backtest_{datetime.now(timezone.utc):%Y%m%d}.csv",
                          "52 Candle Breakout 15 MIN backtest: every trade")
    closed = sum(1 for t in trades if t["exit_at"] is not None)
    print(f"breakout backtest: sent to the private chat -- {closed} closed trades, {len(trades) - closed} open, {passed} passed over, "
          f"{stocks} stocks, "
          f"{first:%d %b %Y} - {last:%d %b %Y}, CSV {'sent' if sent else 'not sent'}")


if __name__ == "__main__":
    asyncio.run(main())
