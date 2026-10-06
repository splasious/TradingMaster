"""
MACD - RSI - 15 MIN (native, unsandboxed) - New Version
============================================

Ported from the standalone MACD/RSI research script (macd_rsi_15_min.py --
kept in Downloads for reference/backtesting) into TradingMaster's native
paper-trading contract. That script's own docstring says "No broker
orders" -- it was a pure batch backtester (call run(bars) once with full
historical DataFrames, get back a simulated ledger). This file is the
live, tick-by-tick equivalent: same signal logic, but incrementally
evaluated each tick against real candles, with real (paper) fills via
ctx.open_leg/close_leg.

Signal -- zero-cross of the MACD LINE itself:
  MACD = EMA(close, FAST) - EMA(close, SLOW)
  buy  = the newest finished candle closes with MACD > 0 and the candle
         before it closed with MACD < 0 -- a fresh up-cross, bought before
         the next candle closes or not at all
  sell = MACD crosses from >0 to <0   (exit)
The MACD signal line (an EMA of MACD) isn't used at all. Until 25-Sep-2026
this strategy traded the signal line's zero-cross instead; the MACD line is
the faster of the two, so it crosses zero earlier -- earlier entries and
exits, and somewhat more trades in choppy markets.
RSI(14) uses Wilder's smoothing, same as the original's rsi14().

Ranking / sizing: among symbols with a fresh up-cross, rank by RSI
descending and fill the free slots up to MAX_POSITIONS, equal-weighted at
1/MAX_POSITIONS of this strategy's own tracked equity (cash + mark-to-market
of current holdings) per slot -- same "fixed decision-time equity,
shrinking cash cap per fill" design as the original run()'s position-sizing
comment, just using live portfolio equity instead of a fixed simulated
CAPITAL constant. A fresh up-cross that finds no free slot is passed over
for good; it's listed in the Last Signal reason.

Revised 6-Oct-2026 -- PCR filter (pcr_filter below):
  - Each time it runs, NIFTY's PCR (the 15-minute record over its next 4
    expiries -- the Options page's PCR -- the latest one, same session, at
    most 30 minutes old).
  - Below PCR_EXIT_BELOW (0.80): every holding is sold at once and nothing
    is bought -- out of the market. Above PCR_REENTER_ABOVE (0.90): back in.
    In between, or with no record, it stays as it was (in or out), so it
    doesn't flip in and out around one level.
  - Back in, it buys only as ever: a fresh up-cross on the newest finished
    candle (decided 6 Oct) -- so it may sit in cash a while after PCR
    recovers. An up-cross while out is listed in the reason, not bought.
  - Before the first PCR record (a backtest of dates before about
    23-Sep-2026) there is no filter: it trades as before.

Revised 30-Sep-2026 -- buy on the up-cross candle only:
  - A symbol used to stay buy-eligible from its up-cross until its next
    down-cross, so a slot freed hours or days later was refilled with a
    stock whose cross was long past. Now only the newest finished candle's
    cross counts, and only until the next candle closes: a cross on the
    day's last candle (15:15-15:30) closes as the market does and isn't
    bought the next morning; one whose candle is saved too late (Zerodha
    logged out, say) is missed.
  - The initial seeding is gone: on its first run a deployment used to fill
    every slot with the highest-RSI watchlist stocks whatever their MACD.
    Every buy now follows the rule above. ("seeded" left in an existing
    deployment's state is no longer read.)
  - Exits are unchanged.

Revised 24-Sep-2026 -- why SOLARINDS was never sold:
  - Exits used to fire only when the down-cross was the NEWEST stored
    candle at the moment a tick looked (sell.iloc[-1]). Miss that one
    moment -- a restart, candles arriving late or several at once -- and
    the position was kept until the MACD crossed up and back down again.
    A holding now exits whenever the MACD's latest zero-cross is a
    down-cross on a candle that closed AFTER it was bought, so a missed
    moment is caught on the next tick (at the price then). If the MACD
    has already crossed back up since, it's held on rather than sold and
    bought straight back. A holding bought while the MACD was already below
    zero (the seeding before 30-Sep-2026) still waits for the next
    down-cross, as before.
  - Candles come from ctx.get_candles(): finished candles only (a candle
    still forming could cross and un-cross), and asking for them keeps
    their 15m candles refreshed in the background -- nothing did that for
    an Advanced deployment's stocks before, so the MACD/RSI here could sit
    frozen at the last manual backfill.
  - Down-crosses inside the first MIN_BARS candles of the window are
    ignored for exits: the EMAs there are still warming up, so a cross
    that early can be an artifact of where the window starts.

Differences from the original research script, deliberately:
  - Uses ctx.portfolio.cash (real paper-portfolio equity) instead of a
    hardcoded CAPITAL=1,000,000 simulation constant.
  - Does NOT model the 0.125%-each-side FEE or slippage -- ctx.open_leg/
    close_leg have no transaction-cost concept. The Closed Trades record
    shows an estimated Charges figure alongside instead.
  - A WATCHLIST symbol with no NSE instrument, or too few candles to
    rank, is listed under "not found" / "insufficient history" in the Last
    Signal reason instead of failing the tick.

FAST/SLOW are kept at the strategy's original 12/26. An earlier sweep
across FAST=8..20 found 9 and 14 at least as strong (see
macd_rsi_15min_fast_sweep_results.csv), but that sweep ran on the
signal-line rule -- re-run it before changing FAST for the MACD-line rule.
"""

import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.services.options.pcr_snapshots import first_record_ts, latest_pcr

WATCHLIST = [
    "ABCAPITAL", "ACUTAAS", "ADANIENSOL", "ADANIPOWER", "AMBER", "ANANDRATHI", "APARINDS", "ASHOKLEY", "ATHERENERG", "AUBANK",
    "BANKINDIA", "BHARATFORG", "BHEL", "BSE", "CANBK", "CUMMINSIND", "DELHIVERY", "EICHERMOT", "FEDERALBNK", "FORTIS",
    "GLENMARK", "GVT&D", "HDFCAMC", "HINDALCO", "HINDCOPPER", "IDEA", "IIFL", "INDIANB", "KARURVYSYA", "LAURUSLABS",
    "LTF", "MANAPPURAM", "MCX", "MFSL", "MUTHOOTFIN", "NATIONALUM", "NAVINFLUOR", "NYKAA", "PAYTM", "POLYCAB",
    "POWERINDIA", "RADICO", "RBLBANK", "SAIL", "SBIN", "SHRIRAMFIN", "SOLARINDS", "TVSMOTOR", "UNIONBANK", "VEDL",
    "CUPID", "HFCL", "KIRLOSENG", "MTARTECH", "STLTECH", "TDPOWERSYS", "WELCORP",  # added 1 Oct
]
TIMEFRAME = "15m"
BAR_LENGTH = timedelta(minutes=15)
MAX_POSITIONS = 5

FAST, SLOW = 12, 26  # see module docstring before changing FAST
RSI_PERIOD = 14
MIN_BARS = 151  # the original script's own warm-up minimum
HISTORY_BARS = 300  # ample warm-up beyond MIN_BARS

PCR_UNDERLYING = "NIFTY"
PCR_EXIT_BELOW = 0.80  # NIFTY PCR under this: sell everything, stay out
PCR_REENTER_ABOVE = 0.90  # over this: back in

IST = timezone(timedelta(hours=5, minutes=30))


def _wilder(x: pd.Series) -> pd.Series:
    seeded = pd.Series(np.nan, index=x.index)
    if len(x) > RSI_PERIOD:
        seeded.iloc[RSI_PERIOD] = x.iloc[1 : RSI_PERIOD + 1].mean()
        seeded.iloc[RSI_PERIOD + 1 :] = x.iloc[RSI_PERIOD + 1 :]
    return seeded.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()


def _rsi(c: pd.Series) -> pd.Series:
    delta = c.diff()
    gain, loss = _wilder(delta.clip(lower=0)), _wilder(-delta.clip(upper=0))
    return (100 - 100 / (1 + gain / loss)).mask((gain == 0) & (loss == 0), 50)


def compute_signal(bars: list[dict]) -> dict | None:
    """Latest MACD-line state from finished candles, oldest -> newest (each
    a dict with "ts" -- when the candle opened -- and "close"). None if there
    isn't enough history yet.

    `buy`: the newest candle is the up-cross (its MACD > 0, the one before
    < 0); `closed_at` is when that candle closed -- entries use the two.
    `last_sell_at` is when the most recent down-cross candle closed (None if
    there's none past the warm-up) -- exits compare it with each holding's
    entry time. `sell` (the newest candle itself is the down-cross) is kept
    for reference only."""
    if len(bars) < MIN_BARS:
        return None
    c = pd.Series([bar["close"] for bar in bars])
    macd = c.ewm(span=FAST, adjust=False, min_periods=FAST).mean() - c.ewm(span=SLOW, adjust=False, min_periods=SLOW).mean()
    buy = (macd.shift() < 0) & (macd > 0)
    sell = (macd.shift() > 0) & (macd < 0)
    active = pd.Series(np.where(buy, 1.0, np.where(sell, 0.0, np.nan))).ffill().fillna(0).eq(1)
    rsi = _rsi(c)
    latest_rsi = rsi.iloc[-1]
    sell_positions = np.flatnonzero(sell.to_numpy()[MIN_BARS - 1 :]) + MIN_BARS - 1
    return {
        "buy": bool(buy.iloc[-1]),
        "closed_at": _as_utc(bars[-1]["ts"]) + BAR_LENGTH,
        "sell": bool(sell.iloc[-1]),
        "active": bool(active.iloc[-1]),
        "rsi": float(latest_rsi) if pd.notna(latest_rsi) else None,
        "last_sell_at": bars[sell_positions[-1]]["ts"] + BAR_LENGTH if len(sell_positions) else None,
    }


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def pcr_filter(ctx) -> tuple[bool, str]:
    """(out of the market?, a note) after the latest PCR -- see "PCR filter"
    above. Kept in state["pcr_risk_off"]."""
    risk_off = bool(ctx.state.get("pcr_risk_off"))
    first = await first_record_ts(ctx.db, PCR_UNDERLYING)
    if first is None or first > as_aware_utc(ctx.now):
        return False, "PCR filter off (no PCR records this far back)"
    pcr = await latest_pcr(ctx.db, PCR_UNDERLYING, as_aware_utc(ctx.now).astimezone(timezone.utc))
    if pcr is not None and pcr < PCR_EXIT_BELOW:
        risk_off = True
    elif pcr is not None and pcr > PCR_REENTER_ABOVE:
        risk_off = False
    ctx.state["pcr_risk_off"] = risk_off
    reading = f"PCR {pcr:.2f}" if pcr is not None else "no PCR record in the last 30 min"
    return risk_off, f"{reading}: {f'out until PCR > {PCR_REENTER_ABOVE:.2f}' if risk_off else 'in the market'}"


async def evaluate(ctx) -> None:
    universe_result = await ctx.db.execute(
        select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(WATCHLIST))
    )
    universe = {row.symbol: row for row in universe_result.scalars().all()}
    missing = [s for s in WATCHLIST if s not in universe]

    signals: dict[str, dict] = {}
    skipped: list[str] = []
    for symbol, instrument in universe.items():
        bars = await ctx.get_candles(instrument.id, TIMEFRAME, HISTORY_BARS)
        sig = compute_signal(bars)
        if sig is None:
            skipped.append(symbol)
            continue
        signals[symbol] = sig

    holdings = ctx.state.get("holdings", {})  # {symbol: {instrument_id, quantity, entry_price, opened_at}}
    risk_off, pcr_note = await pcr_filter(ctx)

    # --- Out on PCR: sell everything now (one with no price yet: next run).
    sold = []
    for symbol in list(holdings.keys()) if risk_off else []:
        leg = holdings[symbol]
        instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        price = (await ctx.get_price(instrument.id)) if instrument else None
        if instrument is None or price is None:
            continue
        holdings.pop(symbol)
        await ctx.close_leg(instrument, "sell", leg["quantity"], price)
        pnl = (price - leg["entry_price"]) * leg["quantity"]
        await ctx.record_trade(
            legs=[{
                "instrument_id": leg["instrument_id"], "side": "long", "quantity": leg["quantity"],
                "entry_price": leg["entry_price"], "exit_price": price,
            }],
            pnl=pnl, pnl_pct=(pnl / (leg["entry_price"] * leg["quantity"]) * 100) if leg["entry_price"] else 0.0,
            exit_reason=f"pcr_below_{PCR_EXIT_BELOW:.2f}", opened_at=datetime.fromisoformat(leg["opened_at"]),
        )
        sold.append(f"{symbol} (PCR)")

    # --- Exits: the latest zero-cross is down, on a candle closing after entry.
    for symbol in [] if risk_off else list(holdings.keys()):
        sig = signals.get(symbol)
        if sig is None or sig["active"] or sig["last_sell_at"] is None:
            continue
        if sig["last_sell_at"] <= _as_utc(datetime.fromisoformat(holdings[symbol]["opened_at"])):
            continue
        leg = holdings.pop(symbol)
        instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        price = (await ctx.get_price(instrument.id)) if instrument else None
        if price is None:
            price = leg["entry_price"]
        await ctx.close_leg(instrument, "sell", leg["quantity"], price)
        pnl = (price - leg["entry_price"]) * leg["quantity"]
        pnl_pct = (pnl / (leg["entry_price"] * leg["quantity"]) * 100) if leg["entry_price"] else 0.0
        await ctx.record_trade(
            legs=[{
                "instrument_id": leg["instrument_id"], "side": "long", "quantity": leg["quantity"],
                "entry_price": leg["entry_price"], "exit_price": price,
            }],
            pnl=pnl, pnl_pct=pnl_pct, exit_reason="macd_zero_cross_down",
            opened_at=datetime.fromisoformat(leg["opened_at"]),
        )
        sold.append(f"{symbol} (MACD < 0 at {sig['last_sell_at'].astimezone(IST):%d-%b %H:%M})")

    # --- Fixed decision-time equity: cash plus mark-to-market of whatever
    # is still held after the exits above -- same convention the original
    # run()'s per-slot sizing used, just against real portfolio equity.
    equity = ctx.portfolio.cash
    for leg in holdings.values():
        price = (await ctx.get_price(uuid.UUID(leg["instrument_id"]))) or leg["entry_price"]
        equity += price * leg["quantity"]

    # --- Entries: a fresh up-cross on the newest finished candle, before the
    # next candle closes -- highest RSI first, up to MAX_POSITIONS.
    eligible = [
        s for s, sig in signals.items()
        if s not in holdings and sig["buy"] and ctx.now < sig["closed_at"] + BAR_LENGTH and sig["rsi"] is not None
    ]
    eligible.sort(key=lambda s: (-signals[s]["rsi"], s))

    bought = []
    no_slot = []
    out_on_pcr = eligible if risk_off else []
    for symbol in [] if risk_off else eligible:
        if len(holdings) >= MAX_POSITIONS:
            no_slot.append(symbol)
            continue
        instrument = universe[symbol]
        price = await ctx.get_price(instrument.id)
        if price is None or price <= 0:
            continue
        # ctx.portfolio.cash here reflects every fill already made this
        # tick (ctx.open_leg mutates it in place) -- correctly shrinks the
        # cap across this loop, while `equity` (the per-slot target) stays
        # fixed at its pre-loop snapshot, exactly like the original run().
        allocation = min(equity / MAX_POSITIONS, ctx.portfolio.cash)
        quantity = float(int(allocation / price))
        if quantity <= 0:
            continue
        await ctx.open_leg(instrument, "buy", quantity, price)
        holdings[symbol] = {
            "instrument_id": str(instrument.id), "quantity": quantity, "entry_price": price,
            "opened_at": ctx.now.isoformat(), "rsi_at_entry": signals[symbol]["rsi"],
        }
        bought.append(f"{symbol} (MACD > 0 at {signals[symbol]['closed_at'].astimezone(IST):%d-%b %H:%M})")

    ctx.state["holdings"] = holdings

    note_bits = [pcr_note]  # first: the reason is cut at 500 characters
    if bought:
        note_bits.append(f"bought: {', '.join(bought)}")
    if sold:
        note_bits.append(f"sold: {', '.join(sold)}")
    if no_slot:
        note_bits.append(f"up-cross, no free slot: {', '.join(no_slot)}")
    if out_on_pcr:
        note_bits.append(f"up-cross, not bought (out on PCR): {', '.join(out_on_pcr)}")
    if missing:
        note_bits.append(f"not found: {', '.join(missing)}")
    if skipped:
        note_bits.append(f"insufficient history: {', '.join(skipped)}")
    note_bits.append(f"holding {len(holdings)}/{MAX_POSITIONS}")

    if bought or sold:
        ctx.note("entered" if bought else "exited", signal="REBALANCE", reason=" | ".join(note_bits))
    else:
        ctx.note("hold", reason=" | ".join(note_bits))
