"""
RS Rotation 15 MIN -- Relative-Strength Rotation on 15-minute candles (native, unsandboxed)
============================================================================================

The weekly Nifty RS Rotation (nifty_rs_rotation.py) run on 15-minute
candles, over its own 50 stocks (STOCK_UNIVERSE below -- the same list as
MACD - RSI - 15 MIN, not the weekly one's 500). At every completed
15-minute candle it ranks them by strength relative to NIFTY 50, with the
same AFL formula:

  ratio  = stock_close / NIFTY_close, per bar
  values = ( ratio / (Sum(ratio, RS_WINDOW) / RS_SUM_DIVISOR) - 1 ) * RS_MULTIPLIER

RS_WINDOW=10 bars now being the last 2.5 hours. RS_SUM_DIVISOR and
RS_MULTIPLIER only rescale the value, never the ranking.

Trading, as decided on 30-Sep-2026:
  - A decision at each 15-minute candle close, 09:30 to 15:15 IST -- 24 a
    day; the 15:15-15:30 candle closes with the market and isn't traded.
    A close is acted on only within DECIDE_WITHIN of it: one missed (a
    restart, Zerodha logged out) is skipped, never traded late.
  - Prices: live, at the close -- each stock's last traded price a few
    seconds after the candle ends (ctx.get_prices: the live feed's ticks,
    else a single Kite request for all of them). That is the newest bar's close
    and the price anything is bought or sold at on it.
  - Earlier bars: the closes this strategy keeps itself (state["_series"],
    private -- not shown on its card), seeded from the stored 15-minute
    candles of the slots before -- the previous session's, downloaded
    each evening, so every morning starts from Kite's final candles. A
    stock is ranked only with all RS_WINDOW bars; one missing a bar waits.
    Started during a session, it ranks once it has RS_WINDOW bars of its
    own, since that day's earlier candles aren't stored until the evening.
  - Buys: while fewer than TOP_N are held, the highest-ranked stocks of
    the top TOP_N not yet held -- so the first TOP_N follow the ranking.
  - Sells: a holding ranked below EXIT_RANK (20) -- a buffer against
    switching back and forth around 10th place; one that couldn't be
    ranked on a bar is kept, not sold on missing data. A holding no longer
    in STOCK_UNIVERSE is sold at the next decision.
  - Sizing: POSITION_SIZE_PCT (10%) of this strategy's own equity (cash
    plus its holdings at the same prices) per buy, never more than the
    cash left.
  - Cash (delivery) positions, held overnight: no end-of-day square-off;
    a sale's proceeds fund the next buy.

Runs through services/paper_trading/native_runner.py -- the same ctx
contract as the other native strategies (ctx.state, ctx.db, ctx.now,
ctx.portfolio, ctx.get_prices, ctx.open_leg, ctx.close_leg,
ctx.record_trade, ctx.note, ctx.wake_at).
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST
from app.services.market_data.nse_holidays import is_trading_holiday

# --- Amend this list to change which stocks are tracked ------------------
STOCK_UNIVERSE = [
    "ABCAPITAL", "ACUTAAS", "ADANIENSOL", "ADANIPOWER", "AMBER", "ANANDRATHI", "APARINDS", "ASHOKLEY", "ATHERENERG", "AUBANK",
    "BANKINDIA", "BHARATFORG", "BHEL", "BSE", "CANBK", "CUMMINSIND", "DELHIVERY", "EICHERMOT", "FEDERALBNK", "FORTIS",
    "GLENMARK", "GVT&D", "HDFCAMC", "HINDALCO", "HINDCOPPER", "IDEA", "IIFL", "INDIANB", "KARURVYSYA", "LAURUSLABS",
    "LTF", "MANAPPURAM", "MCX", "MFSL", "MUTHOOTFIN", "NATIONALUM", "NAVINFLUOR", "NYKAA", "PAYTM", "POLYCAB",
    "POWERINDIA", "RADICO", "RBLBANK", "SAIL", "SBIN", "SHRIRAMFIN", "SOLARINDS", "TVSMOTOR", "UNIONBANK", "VEDL",
]
BENCHMARK_SYMBOL = "NIFTY 50"

TIMEFRAME = "15m"
BAR = timedelta(minutes=15)
FIRST_BAR = time(9, 15)  # the session's first candle opens
LAST_BAR = time(15, 15)  # and its last; it closes with the market, so isn't traded
DECISIONS_PER_SESSION = 24  # closes 09:30 ... 15:15
DECIDE_WITHIN = timedelta(minutes=2)
WAKE_AFTER_CLOSE = timedelta(seconds=3)  # the candle's last trades are in by then

TOP_N = 10
EXIT_RANK = 20
POSITION_SIZE_PCT = 10.0

RS_WINDOW = 10
RS_SUM_DIVISOR = 13.0
RS_MULTIPLIER = 12.0


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_trading_holiday(d)


def _next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not _is_trading_day(d):
        d += timedelta(days=1)
    return d


def _previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _session_start(d: date) -> datetime:
    return datetime.combine(d, FIRST_BAR, tzinfo=IST)


def just_closed_bar(now: datetime) -> datetime | None:
    """Start (IST) of the candle that has just closed, if it's one decided
    on (closing 09:30-15:15) and closed no more than DECIDE_WITHIN ago."""
    now = now.astimezone(IST)
    if not _is_trading_day(now.date()):
        return None
    start = _session_start(now.date())
    closed = int((now - start) // BAR)  # candles closed so far today
    if not 1 <= closed <= DECISIONS_PER_SESSION:
        return None
    close = start + closed * BAR
    return close - BAR if now - close <= DECIDE_WITHIN else None


def next_close(now: datetime) -> datetime:
    """The next close decided on, after `now` (IST)."""
    now = now.astimezone(IST)
    if _is_trading_day(now.date()):
        start = _session_start(now.date())
        closed = max(int((now - start) // BAR), 0) if now >= start else 0
        if closed < DECISIONS_PER_SESSION:
            return start + (closed + 1) * BAR
    return _session_start(_next_trading_day(now.date())) + BAR


def bars_before(bar: datetime, count: int) -> list[datetime]:
    """The `count` candle starts before `bar` (IST), oldest first, back
    across sessions -- every candle, 09:15 to 15:15."""
    out: list[datetime] = []
    t = bar.astimezone(IST)
    while len(out) < count:
        t -= BAR
        if t.time() < FIRST_BAR:
            t = datetime.combine(_previous_trading_day(t.date()), LAST_BAR, tzinfo=IST)
        out.append(t)
    return out[::-1]


def rs_value(stock: list, bench: list) -> float | None:
    """The AFL value on the last RS_WINDOW bars -- None unless every one has
    both closes."""
    if len(stock) < RS_WINDOW or len(bench) < RS_WINDOW:
        return None
    pairs = list(zip(stock[-RS_WINDOW:], bench[-RS_WINDOW:]))
    if any(s is None or not b for s, b in pairs):
        return None
    ratios = [s / b for s, b in pairs]
    rolling = sum(ratios) / RS_SUM_DIVISOR
    return (ratios[-1] / rolling - 1) * RS_MULTIPLIER if rolling else None


async def _seeded_series(ctx, bar: datetime, instruments: dict) -> dict:
    """The RS_WINDOW-1 slots before `bar` from the stored 15-minute candles
    -- None where a stock has none. `instruments`: {symbol: Instrument}."""
    slots = bars_before(bar, RS_WINDOW - 1)
    ids = {inst.id: symbol for symbol, inst in instruments.items()}
    rows = (
        await ctx.db.execute(
            select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.close).where(
                OhlcvCandle.instrument_id.in_(list(ids)), OhlcvCandle.timeframe == TIMEFRAME,
                OhlcvCandle.ts >= slots[0].astimezone(timezone.utc), OhlcvCandle.ts <= slots[-1].astimezone(timezone.utc),
            )
        )
    ).all()
    stored = {(ids[instrument_id], as_aware_utc(ts)): close for instrument_id, ts, close in rows}
    return {
        "bars": [s.isoformat() for s in slots],
        "closes": {symbol: [stored.get((symbol, s)) for s in slots] for symbol in instruments},
    }


async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    holdings = ctx.state.get("holdings", {})  # {symbol: {instrument_id, quantity, entry_price, opened_at, rank}}
    series = ctx.state.get("_series") or {"bars": [], "closes": {}}
    upcoming = next_close(now_ist)
    ctx.wake_at(upcoming + WAKE_AFTER_CLOSE)

    bar = just_closed_bar(now_ist)
    if bar is None or (series["bars"] and series["bars"][-1] == bar.isoformat()):
        ctx.note("hold", reason=f"holding {len(holdings)}/{TOP_N} | next decision {upcoming:%d-%b %H:%M}")
        return
    close_label = f"{bar + BAR:%H:%M}"

    benchmark = (await ctx.db.execute(select(Instrument).where(Instrument.symbol == BENCHMARK_SYMBOL))).scalar_one_or_none()
    if benchmark is None:
        ctx.note("skipped", reason=f"{BENCHMARK_SYMBOL} instrument not found")
        return
    universe = {
        row.symbol: row for row in (
            await ctx.db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(STOCK_UNIVERSE)))
        ).scalars()
    }
    instruments = {**universe, BENCHMARK_SYMBOL: benchmark}
    held_ids = [uuid.UUID(leg["instrument_id"]) for leg in holdings.values()]
    prices = await ctx.get_prices([inst.id for inst in instruments.values()] + held_ids)
    if prices.get(benchmark.id) is None:
        # Not marked done: the next tick tries again, while still within DECIDE_WITHIN.
        ctx.note("skipped", reason=f"no live {BENCHMARK_SYMBOL} price at the {close_label} close yet")
        return

    # --- The rolling closes, extended by this bar's live prices. ----------
    expected_before = bars_before(bar, 1)[0].isoformat()
    if not series["bars"] or series["bars"][-1] != expected_before:
        series = await _seeded_series(ctx, bar, instruments)
    series["bars"] = (series["bars"] + [bar.isoformat()])[-RS_WINDOW:]
    closes = {}
    for symbol, inst in instruments.items():
        price = prices.get(inst.id)
        closes[symbol] = (series["closes"].get(symbol, []) + [round(price, 2) if price else None])[-RS_WINDOW:]
    series["closes"] = closes

    bench = closes[BENCHMARK_SYMBOL]
    scores = {symbol: v for symbol in universe if (v := rs_value(closes[symbol], bench)) is not None}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    rank = {symbol: i + 1 for i, (symbol, _) in enumerate(ranked)}

    # --- Sells: ranked below EXIT_RANK, or no longer in the universe. ------
    sold = []
    for symbol in list(holdings):
        leg = holdings[symbol]
        out_of_universe = symbol not in universe
        if not out_of_universe and (symbol not in rank or rank[symbol] <= EXIT_RANK):
            continue
        instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        price = prices.get(uuid.UUID(leg["instrument_id"]))
        if instrument is None or price is None:
            continue  # nothing to sell against at this close -- tried again at the next
        holdings.pop(symbol)
        await ctx.close_leg(instrument, "sell", leg["quantity"], price)
        pnl = (price - leg["entry_price"]) * leg["quantity"]
        await ctx.record_trade(
            legs=[{
                "instrument_id": leg["instrument_id"], "side": "long", "quantity": leg["quantity"],
                "entry_price": leg["entry_price"], "exit_price": price,
            }],
            pnl=pnl, pnl_pct=(pnl / (leg["entry_price"] * leg["quantity"]) * 100) if leg["entry_price"] else 0.0,
            exit_reason="left_universe" if out_of_universe else f"rank_below_{EXIT_RANK}",
            opened_at=datetime.fromisoformat(leg["opened_at"]),
        )
        sold.append(f"{symbol} ({'not in list' if out_of_universe else f'rank {rank[symbol]}'})")

    # --- Buys: the top TOP_N not held, highest first, into the free slots. --
    equity = ctx.portfolio.cash + sum(
        prices.get(uuid.UUID(leg["instrument_id"]), leg["entry_price"]) * leg["quantity"] for leg in holdings.values()
    )
    bought = []
    for symbol, _ in ranked[:TOP_N]:
        if len(holdings) >= TOP_N:
            break
        if symbol in holdings:
            continue
        instrument, price = universe[symbol], prices.get(universe[symbol].id)
        if not price:
            continue
        # ctx.portfolio.cash reflects every fill so far this close (open_leg moves it).
        quantity = float(int(min(equity * POSITION_SIZE_PCT / 100, ctx.portfolio.cash) / price))
        if quantity <= 0:
            continue
        await ctx.open_leg(instrument, "buy", quantity, price)
        holdings[symbol] = {
            "instrument_id": str(instrument.id), "quantity": quantity, "entry_price": price,
            "opened_at": ctx.now.isoformat(), "rank": rank[symbol], "rs_value": scores[symbol],
        }
        bought.append(f"{symbol} (rank {rank[symbol]})")

    ctx.state["holdings"] = holdings
    ctx.state["_series"] = series
    ranked_note = f"ranked {len(ranked)}/{len(universe)}" if len(ranked) >= TOP_N else (
        f"warming up: {len(ranked)}/{len(universe)} stocks have {RS_WINDOW} bars"
    )
    reason = (
        f"{close_label} close: bought: {', '.join(bought) or 'none'} | sold: {', '.join(sold) or 'none'} | "
        f"holding {len(holdings)}/{TOP_N} | {ranked_note} | next decision {upcoming:%H:%M}"
    )
    if bought or sold:
        ctx.note("entered" if bought else "exited", signal="REBALANCE", reason=reason)
    else:
        ctx.note("hold", reason=reason)
