"""
F&O Opening-Candle Momentum Scanner -- FLY OI SCN, version 6 (native)
=====================================================================

Scans every F&O stock (each stock with an unexpired NFO future, ~212) at
the open, buys a ~2% OTM option of the stocks that break out of their
09:15-09:25 range, and trails the trade with the stock's 8-SMA.

  09:20:07  Watchlist. Every stock's price and previous close from one Kite
            quote pass, then for the stocks that moved:
              1. Move      |move vs Kite's previous close| > 2%
                           (up -> CE, down -> PE)
              2. Total OI  current-month future + every CE + every PE of
                           that expiry: live now vs yesterday's close
                           (captured at 15:31, services/fo_scan/oi_store.py)
                           -- must have RISEN by more than 7%
              3. Candle    the 09:15-09:20 candle mustn't have given back 50%
                           or more of its range (close vs its high for a CE,
                           vs its low for a PE)
            A stock whose 09:15 candle isn't published yet is retried every
            3 seconds until 09:25:07.
  09:25:07  Final shortlist.
              4. Nifty     the 09:15-09:25 candle: green (close > open)
                           keeps gainers and losers, otherwise losers only
            Then the same scan again for stocks that weren't on the
            watchlist (a late mover can still qualify), and each shortlisted
            stock's 09:15-09:25 high and low -- its breakout levels.
  09:25-10:30  Every 5 seconds, one quote pass over the shortlist:
              5. Breakout  above the high (CE) / below the low (PE) -> buy
                           1 lot of the current-month option nearest 2% OTM
                           at its live price
              6. Limit     3 trades a day, the first three breakouts
              7. Blackout  no trade on expiry day, the 2 trading days
                           before it or the 2 after the previous one
            No breakout by 10:30 -> no trade.
  Exit      Two consecutive 5-minute closes of the stock on the wrong side
            of its 8-SMA (below for a CE, above for a PE), the SMA taken as
            a chart draws it -- over the last 8 candles, yesterday's
            included -- and both candles closing after the entry. Checked 7
            seconds after every 5-minute close. Anything still open is
            closed at 15:10.

The runner calls evaluate() every 10 seconds; the exact times above come
from ctx.wake_at (services/paper_trading/scheduler.py). Every read is live
Kite data, paced to Kite's limits (services/fo_scan/pacing.py): one /quote
request a second, three history requests.

Alerts (in-app + Telegram), each led by an "As of <IST time>" line: one per
stock put on the watchlist, a 09:20 summary with every stock that moved but
was rejected and why, the 09:25 final shortlist, each entry and exit, and
the 15:10 report. Every stock's result in each scan is saved to
fo_scan_results (models/fo_scan.py) for later analysis.

Needs a connected Zerodha account; a historical replay (backtest) isn't
supported -- the rules run on live quotes and on OI history that only
exists from the day the OI store started capturing.
"""

import logging
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.core.time import as_aware_utc
from app.models.alert import AlertSeverity, AlertType
from app.models.fo_scan import FoScanResult
from app.models.instrument import Instrument
from app.services.alerts.service import create_alert
from app.services.backfill_platform.coverage import is_trading_day, previous_trading_day
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import IST, KiteAPIError, ZerodhaKiteBroker
from app.services.fo_scan import oi_store
from app.services.fo_scan.pacing import kite_history, kite_quotes
from app.services.market_data.tick_engine import tick_engine
from app.services.notifications.telegram import send_telegram

logger = logging.getLogger(__name__)

VERSION = 6

# ---------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------
MOMENTUM_PCT = 2.0
OI_RISE_PCT = 7.0
MAX_RETRACEMENT_PCT = 50.0
OTM_PCT = 2.0
MAX_OI_STRIKE_BAND_PCT = 10.0  # informational "highest OI strike near spot"
SMA_PERIOD = 8
SMA_CONFIRM = 2
EXPIRY_BLACKOUT_DAYS = 2
MAX_TRADES_PER_DAY = 3
LOTS_PER_SETUP = 1
DEFAULT_LOT_SIZE = 1

# ---------------------------------------------------------------------
# Times (IST)
# ---------------------------------------------------------------------
MARKET_OPEN = dtime(9, 15)
WARM_UP = dtime(9, 19, 30)  # Kite's NSE instrument list, so 09:20:07 doesn't wait for it
FIRST_SCAN = dtime(9, 20, 7)
SECOND_SCAN = dtime(9, 25, 7)
NIFTY_GIVE_UP = dtime(9, 30)
BREAKOUT_CUTOFF = dtime(10, 30)
REPORT_TIME = dtime(15, 10)
CANDLE = timedelta(minutes=5)
CANDLE_SETTLE = timedelta(seconds=7)
BREAKOUT_POLL = timedelta(seconds=5)
RETRY = timedelta(seconds=3)
# How far back the exit's SMA reaches for yesterday's candles.
SMA_HISTORY_FROM = dtime(14, 15)
NIFTY_SYMBOL = "NIFTY 50"

SCAN_1 = "09:20"
SCAN_2 = "09:25"

# Setup statuses.
WATCHLIST = "watchlist"  # passed 09:20/09:25 rules 1-3, waiting for the Nifty bias
WATCHING = "watching"  # final shortlist, waiting for a breakout
DROPPED_NIFTY = "dropped_nifty"
NO_TRIGGER = "no_trigger"
BLACKOUT = "blackout"
LIMIT_REACHED = "limit_reached"
TRIGGERED = "triggered"
EXITED = "exited"
EOD_CLOSED = "eod_closed"


# ---------------------------------------------------------------------
# Rules as plain functions
# ---------------------------------------------------------------------
def momentum_direction(pct_change: float) -> str:
    return "CE" if pct_change > 0 else "PE"


def passes_momentum(pct_change: float) -> bool:
    return abs(pct_change) > MOMENTUM_PCT


def passes_oi_rise(oi_pct_change: float | None) -> bool:
    """Only a RISE counts: rising OI is new positions being built."""
    return oi_pct_change is not None and oi_pct_change > OI_RISE_PCT


def retracement_pct(high: float, low: float, close: float, direction: str) -> float:
    """How much of the candle's range its close gave back from the extreme
    in the trade's direction (the high for a CE, the low for a PE)."""
    candle_range = high - low
    if candle_range <= 0:
        return 0.0
    if direction == "CE":
        return (high - close) / candle_range * 100.0
    return (close - low) / candle_range * 100.0


def passes_retracement(high: float, low: float, close: float, direction: str) -> bool:
    return retracement_pct(high, low, close, direction) < MAX_RETRACEMENT_PCT


def nifty_bias(open_: float, close: float) -> str:
    return "green" if close > open_ else "red"


def nifty_allows(direction: str, bias: str) -> bool:
    return bias == "green" or direction == "PE"


def sma_series(closes: list[float], period: int = SMA_PERIOD) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(closes)):
        out.append(None if i + 1 < period else sum(closes[i + 1 - period : i + 1]) / period)
    return out


def sma_exit_due(direction: str, bars: list[dict], entered_at: datetime) -> bool:
    """True when the last SMA_CONFIRM completed 5-minute candles all closed
    on the wrong side of the SMA and all closed after `entered_at`.
    `bars`: [{"ts": candle start, "close": ...}], oldest first, yesterday's
    included so the SMA is the one a chart shows."""
    if len(bars) < SMA_PERIOD + SMA_CONFIRM - 1:
        return False
    sma = sma_series([b["close"] for b in bars])
    recent = list(zip(bars[-SMA_CONFIRM:], sma[-SMA_CONFIRM:]))
    if any(s is None or as_aware_utc(b["ts"]) + CANDLE <= as_aware_utc(entered_at) for b, s in recent):
        return False
    if direction == "CE":
        return all(b["close"] < s for b, s in recent)
    return all(b["close"] > s for b, s in recent)


def trading_days_between(start: date, end: date) -> int:
    """Trading days after `start`, up to and including `end`."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


def in_expiry_blackout(today: date, expiry: date, previous_expiry: date | None, days: int = EXPIRY_BLACKOUT_DAYS) -> bool:
    """Expiry day, the `days` trading days before it, and the `days` after
    the previous expiry. NSE holidays aren't trading days."""
    if today == expiry or trading_days_between(today, expiry) <= days:
        return True
    return previous_expiry is not None and trading_days_between(previous_expiry, today) <= days


def monthly_expiry(year: int, month: int) -> date:
    """NSE's monthly stock-derivative expiry: the last Tuesday of the month,
    the trading day before it when that's a holiday."""
    d = (date(year + (month == 12), month % 12 + 1, 1)) - timedelta(days=1)
    while d.weekday() != 1:
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_monthly_expiry_before(today: date) -> date:
    e = monthly_expiry(today.year, today.month)
    if e < today:
        return e
    first = today.replace(day=1) - timedelta(days=1)
    return monthly_expiry(first.year, first.month)


def total_oi(legs: list[oi_store.Contract], prev: dict, now: dict) -> dict | None:
    """Total OI = future + every CE + every PE. A contract counts only when
    both its readings are known -- one with only today's OI would move the
    total by its whole OI, not by its change. None without a baseline."""
    sums = {"FUT": [0.0, 0.0], "CE": [0.0, 0.0], "PE": [0.0, 0.0]}
    counted = {"FUT": 0, "CE": 0, "PE": 0}
    for leg in legs:
        p, n = prev.get(leg.instrument_id), now.get(leg.instrument_id)
        if p is None or n is None:
            continue
        sums[leg.kind][0] += p
        sums[leg.kind][1] += n
        counted[leg.kind] += 1
    prev_total = sum(v[0] for v in sums.values())
    now_total = sum(v[1] for v in sums.values())
    if not prev_total:
        return None
    listed = {k: sum(1 for leg in legs if leg.kind == k) for k in sums}
    return {
        "pct_change": (now_total - prev_total) / prev_total * 100.0, "prev_total": prev_total, "now_total": now_total,
        "fut_prev": sums["FUT"][0], "fut_now": sums["FUT"][1], "ce_prev": sums["CE"][0], "ce_now": sums["CE"][1],
        "pe_prev": sums["PE"][0], "pe_now": sums["PE"][1],
        "fut_counted": counted["FUT"], "ce_counted": counted["CE"], "ce_listed": listed["CE"],
        "pe_counted": counted["PE"], "pe_listed": listed["PE"],
        "counted": sum(counted.values()), "listed": len(legs),
    }


def pick_otm(options: list, direction: str, spot: float):
    """The option (Instrument or Contract) whose strike is nearest ~2% OTM."""
    target = spot * (1 + OTM_PCT / 100.0) if direction == "CE" else spot * (1 - OTM_PCT / 100.0)
    candidates = [o for o in options if o.strike is not None]
    return min(candidates, key=lambda o: abs(o.strike - target)) if candidates else None


def max_oi_strike_near_spot(legs: list[oi_store.Contract], now: dict, spot: float, kind: str) -> tuple[float, float] | None:
    lo, hi = spot * (1 - MAX_OI_STRIKE_BAND_PCT / 100.0), spot * (1 + MAX_OI_STRIKE_BAND_PCT / 100.0)
    best = None
    for leg in legs:
        if leg.kind == kind and leg.strike is not None and lo <= leg.strike <= hi and now.get(leg.instrument_id) is not None:
            if best is None or now[leg.instrument_id] > best[1]:
                best = (leg.strike, now[leg.instrument_id])
    return best


# ---------------------------------------------------------------------
# Kite and catalog reads
# ---------------------------------------------------------------------
def _at(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def _key(instrument: Instrument) -> str:
    return f"{instrument.exchange}:{instrument.external_ref}"


async def _live_broker(ctx) -> ZerodhaKiteBroker | None:
    creds = await find_connected_zerodha_credentials(ctx.db)
    if creds is None:
        return None
    broker = ZerodhaKiteBroker()
    broker._api_key = creds["api_key"]
    broker._access_token = creds["access_token"]
    return broker


async def _candles(broker, instrument: Instrument, start: datetime, end: datetime) -> dict[datetime, dict]:
    """5-minute candles by start time (UTC); {} if Kite fails."""
    try:
        bars = await kite_history(broker, instrument.external_ref, "5m", start, end, instrument.exchange)
    except KiteAPIError as exc:
        logger.warning("FLY OI SCN: 5m candles failed for %s: %s", instrument.symbol, exc)
        return {}
    return {as_aware_utc(b["ts"]).astimezone(timezone.utc): b for b in bars}


async def _universe(ctx, today: date) -> list[tuple[Instrument, Instrument]]:
    """(equity, nearest unexpired future) for every F&O stock."""
    futures = await oi_store.stock_futures(ctx.db, today)
    if not futures:
        return []
    equities = (await ctx.db.execute(select(Instrument).where(Instrument.id.in_(list(futures))))).scalars().all()
    return sorted(((eq, futures[eq.id][0]) for eq in equities), key=lambda pair: pair[0].symbol)


async def _options(ctx, equity_id: uuid.UUID, expiry: date, option_type: str) -> list[Instrument]:
    return list(
        (
            await ctx.db.execute(
                select(Instrument).where(
                    Instrument.instrument_type == "option", Instrument.underlying_instrument_id == equity_id,
                    Instrument.expiry == expiry, Instrument.option_type == option_type, Instrument.strike.is_not(None),
                )
            )
        ).scalars().all()
    )


async def _previous_expiry(ctx, equity_id: uuid.UUID, today: date) -> date:
    known = (
        await ctx.db.execute(
            select(func.max(Instrument.expiry)).where(
                Instrument.instrument_type == "future", Instrument.underlying_instrument_id == equity_id, Instrument.expiry < today,
            )
        )
    ).scalar_one_or_none()
    return known or last_monthly_expiry_before(today)


async def _ltp(broker, instrument: Instrument) -> float | None:
    quotes, _ = await kite_quotes(broker, [_key(instrument)])
    price = (quotes.get(_key(instrument)) or {}).get("last_price")
    return float(price) if price else None


# ---------------------------------------------------------------------
# State, alerts, records
# ---------------------------------------------------------------------
def _fresh_state(today: date) -> dict:
    return {
        "version": VERSION, "session_date": today.isoformat(), "warmed": False,
        "scans": {SCAN_1: {"status": "pending"}, SCAN_2: {"status": "pending"}},
        "nifty": None, "setups": {}, "trades_today": 0, "last_poll_at": None, "last_exit_check": None,
        "report_sent": False, "login_alert_sent": False,
    }


def _stamp(now_ist: datetime) -> str:
    return f"As of {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST"


async def _notify(ctx, title: str, message: str, *, alert_type: str = AlertType.STRATEGY_SIGNAL.value, severity: AlertSeverity = AlertSeverity.INFO) -> None:
    await create_alert(
        ctx.db, user_id=ctx.portfolio.user_id, alert_type=alert_type, severity=severity, title=title[:200], message=message[:1000],
        object_type="paper_native_deployment", object_id=str(ctx.deployment.id),
    )
    await send_telegram(title, message)


async def _save_rows(ctx, today: date, scan: str, rows: dict[str, dict]) -> None:
    """One fo_scan_results row per stock of this scan (replacing any from
    an earlier, interrupted attempt)."""
    if not rows:
        return
    await ctx.db.execute(
        delete(FoScanResult)
        .where(
            FoScanResult.deployment_id == ctx.deployment.id, FoScanResult.session_date == today,
            FoScanResult.scan == scan, FoScanResult.symbol.in_(list(rows)),
        )
        .execution_options(synchronize_session=False)
    )
    for symbol, row in rows.items():
        ctx.db.add(FoScanResult(deployment_id=ctx.deployment.id, session_date=today, scan=scan, symbol=symbol, **row))


async def _update_row(ctx, today: date, symbol: str, setup: dict, **fields) -> None:
    row = (
        await ctx.db.execute(
            select(FoScanResult).where(
                FoScanResult.deployment_id == ctx.deployment.id, FoScanResult.session_date == today,
                FoScanResult.scan == setup["scan"], FoScanResult.symbol == symbol,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        for name, value in fields.items():
            setattr(row, name, value)


def _fmt_oi(oi: dict | None) -> str:
    if not oi:
        return "Total OI: no baseline (yesterday's close OI not on file)"
    return (
        f"Total OI (future + all CE + all PE, current month): yesterday {oi['prev_total']:,.0f} -> now {oi['now_total']:,.0f} "
        f"({oi['pct_change']:+.1f}%, needs a rise above {OI_RISE_PCT:.0f}%)"
        f"\n  Future: {oi['fut_prev']:,.0f} -> {oi['fut_now']:,.0f} | "
        f"CE ({oi['ce_counted']}/{oi['ce_listed']} strikes): {oi['ce_prev']:,.0f} -> {oi['ce_now']:,.0f} | "
        f"PE ({oi['pe_counted']}/{oi['pe_listed']} strikes): {oi['pe_prev']:,.0f} -> {oi['pe_now']:,.0f}"
    )


def _fmt_stock(symbol: str, s: dict) -> str:
    line = (
        f"{symbol} {s['direction']}: cash {s['prev_close']:.2f} -> {s['price_at_scan']:.2f} "
        f"({s['move_pct']:+.2f}%, needs beyond +/-{MOMENTUM_PCT:.0f}%)\n  {_fmt_oi(s.get('oi'))}"
    )
    if s.get("retrace_pct") is not None:
        line += f"\n  9:15-9:20 candle retraced {s['retrace_pct']:.0f}% (needs under {MAX_RETRACEMENT_PCT:.0f}%)"
    if s.get("preview_option"):
        line += f"\n  Trade strike now: {s['preview_option']} (~2% OTM at scan price)"
    ce, pe = s.get("max_oi_ce"), s.get("max_oi_pe")
    if ce or pe:
        line += "\n  Highest OI strike near spot: " + (f"CE {ce[0]:.0f} ({ce[1]:,.0f})" if ce else "CE --") + " " + (f"PE {pe[0]:.0f} ({pe[1]:,.0f})" if pe else "PE --")
    if s.get("blackout"):
        line += "\n  Expiry blackout today: scanned and recorded, no trade"
    return line


def _fmt_rejected(symbol: str, row: dict) -> str:
    oi = f"Total OI {row['oi_change_pct']:+.1f}%" if row.get("oi_change_pct") is not None else "Total OI n/a"
    return f"{symbol} {row.get('direction') or ''}: {row['move_pct']:+.2f}%, {oi} -- {'; '.join(row['reasons'])}"


# ---------------------------------------------------------------------
# Scans (09:20:07 and 09:25:07)
# ---------------------------------------------------------------------
def _base_row(equity: Instrument, now: datetime) -> dict:
    return {"underlying_id": equity.id, "passed_move": False, "outcome": "rejected", "reasons": [], "scanned_at": now}


async def _scan(ctx, broker, today: date, now_ist: datetime, scan: str) -> dict:
    """Rules 1-3 over every F&O stock not already on the list. Returns
    {"rows", "listed", "pending", "counts", "data_source"}: `listed` are the
    stocks that passed, `pending` the movers whose 09:15 candle Kite hasn't
    published yet (their row still open)."""
    now_utc = now_ist.astimezone(timezone.utc)
    setups = ctx.state["setups"]
    universe = [(eq, fut) for eq, fut in await _universe(ctx, today) if eq.symbol not in setups]
    quotes, quote_error = await kite_quotes(broker, [_key(eq) for eq, _ in universe])
    counts = {"scanned": len(universe), "no_price": 0, "below_move": 0, "movers": 0}
    data_source = "Kite live quotes" + (f" (some requests failed: {quote_error})" if quote_error else "")
    if universe and not quotes:
        return {"rows": {}, "listed": {}, "pending": {}, "counts": counts, "data_source": f"no Kite quotes ({quote_error or 'none returned'})", "failed": True}

    rows: dict[str, dict] = {}
    movers: list[tuple[Instrument, Instrument, dict]] = []
    for equity, future in universe:
        row = _base_row(equity, now_utc)
        rows[equity.symbol] = row
        q = quotes.get(_key(equity)) or {}
        price, prev_close = q.get("last_price"), (q.get("ohlc") or {}).get("close")
        if not price or not prev_close:
            counts["no_price"] += 1
            row["reasons"] = ["no live price or previous close"]
            continue
        move = (float(price) - float(prev_close)) / float(prev_close) * 100.0
        row.update(price=float(price), prev_close=float(prev_close), move_pct=move, direction=momentum_direction(move))
        if not passes_momentum(move):
            counts["below_move"] += 1
            row["reasons"] = [f"moved {move:+.2f}% (needs beyond +/-{MOMENTUM_PCT:.0f}%)"]
            continue
        row["passed_move"] = True
        movers.append((equity, future, row))
    counts["movers"] = len(movers)

    # Rule 2: every mover's current-month future + CE + PE, live, in as few
    # quote requests as possible; yesterday's side from the OI store.
    legs_by_stock: dict[uuid.UUID, list[oi_store.Contract]] = {}
    for leg in await oi_store.stock_contracts(ctx.db, today, underlying_ids={eq.id for eq, _, _ in movers}):
        legs_by_stock.setdefault(leg.underlying_id, []).append(leg)
    all_legs = [leg for legs in legs_by_stock.values() for leg in legs]
    oi_quotes, _ = await kite_quotes(broker, [leg.key for leg in all_legs])
    now_oi = {
        leg.instrument_id: float(oi_quotes[leg.key]["oi"])
        for leg in all_legs if (oi_quotes.get(leg.key) or {}).get("oi") is not None
    }

    listed: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    for equity, future, row in movers:
        legs = legs_by_stock.get(equity.id, [])
        prev, baseline = await oi_store.previous_close(ctx.db, today, [leg.instrument_id for leg in legs])
        if not prev and legs:
            prev = await oi_store.save_daily_candle_baseline(ctx.db, broker, today, legs)
            baseline = oi_store.BASELINE_DAILY_CANDLE if prev else None
        oi = total_oi(legs, prev, now_oi)
        row.update(oi_baseline=baseline, legs_listed=len(legs), legs_counted=oi["counted"] if oi else 0)
        if oi:
            row.update(
                oi_prev_total=oi["prev_total"], oi_now_total=oi["now_total"], oi_change_pct=oi["pct_change"],
                fut_prev=oi["fut_prev"], fut_now=oi["fut_now"], ce_prev=oi["ce_prev"], ce_now=oi["ce_now"],
                pe_prev=oi["pe_prev"], pe_now=oi["pe_now"],
            )
        row["passed_oi"] = passes_oi_rise(oi["pct_change"] if oi else None)
        if oi is None:
            row["reasons"].append("no Total OI baseline (yesterday's close OI not on file)")
        elif not row["passed_oi"]:
            row["reasons"].append(f"Total OI {oi['pct_change']:+.1f}% (needs a rise above {OI_RISE_PCT:.0f}%)")

        expiry = future.expiry
        previous_expiry = await _previous_expiry(ctx, equity.id, today)
        setup = {
            "equity_instrument_id": str(equity.id), "future_expiry": expiry.isoformat(), "scan": scan,
            "direction": row["direction"], "status": WATCHLIST, "prev_close": row["prev_close"],
            "price_at_scan": row["price"], "move_pct": row["move_pct"], "oi": oi, "oi_baseline": baseline,
            "retrace_pct": None, "blackout": in_expiry_blackout(today, expiry, previous_expiry),
            "breakout_high": None, "breakout_low": None, "trigger_time": None, "trigger_spot": None,
            "option_instrument_id": None, "option_symbol": None, "lot_size": None, "entry_premium": None,
            "exit_time": None, "exit_premium": None, "exit_reason": None, "pnl": None,
            "max_oi_ce": max_oi_strike_near_spot(legs, now_oi, row["price"], "CE"),
            "max_oi_pe": max_oi_strike_near_spot(legs, now_oi, row["price"], "PE"),
        }
        preview = pick_otm([leg for leg in legs if leg.kind == row["direction"]], row["direction"], row["price"])
        setup["preview_option"] = preview.tradingsymbol if preview else None
        row["_setup"] = setup
        if not await _judge_candle(ctx, broker, today, equity, row):
            row["outcome"] = "pending"
            pending[equity.symbol] = row
            continue
        _close_row(row)
        if row["outcome"] == WATCHLIST:
            listed[equity.symbol] = setup
    return {"rows": rows, "listed": listed, "pending": pending, "counts": counts, "data_source": data_source, "failed": False}


async def _judge_candle(ctx, broker, today: date, equity: Instrument, row: dict) -> bool:
    """Rule 3 on the stock's 09:15-09:20 candle. False if Kite hasn't
    published it yet."""
    bars = await _candles(broker, equity, _at(today, MARKET_OPEN), _at(today, dtime(9, 20)))
    bar = bars.get(_at(today, MARKET_OPEN).astimezone(timezone.utc))
    if bar is None:
        return False
    direction = row["direction"]
    retraced = retracement_pct(bar["high"], bar["low"], bar["close"], direction)
    row["retrace_pct"] = retraced
    row["_setup"]["retrace_pct"] = retraced
    row["passed_retrace"] = retraced < MAX_RETRACEMENT_PCT
    if not row["passed_retrace"]:
        row["reasons"].append(f"9:15-9:20 candle retraced {retraced:.0f}% (needs under {MAX_RETRACEMENT_PCT:.0f}%)")
    return True


def _close_row(row: dict) -> None:
    """A mover whose three rules are all judged: on the watchlist or not."""
    row["outcome"] = WATCHLIST if not row["reasons"] else "rejected"


def _pending_entry(row: dict) -> dict:
    """What a mover waiting for its 09:15 candle needs kept in the
    (JSON) state until the retry."""
    keys = ("direction", "reasons", "passed_move", "move_pct", "oi_change_pct", "_setup")
    return {k: row[k] for k in keys if k in row}


def _row_for_db(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


async def _finish_scan_1(ctx, today: date, now_ist: datetime, result: dict) -> None:
    info = ctx.state["scans"][SCAN_1]
    for symbol, setup in result["listed"].items():
        ctx.state["setups"][symbol] = setup
        await _notify(ctx, f"FLY OI SCN: {symbol} {setup['direction']} on the watchlist", f"{_stamp(now_ist)}\n\n{_fmt_stock(symbol, setup)}")
    rows = info.pop("rows_summary", None) or {}
    rejected = [(s, r) for s, r in rows.items() if r["passed_move"] and r["reasons"]]
    rejected.sort(key=lambda sr: (len(sr[1]["reasons"]), -abs(sr[1]["move_pct"])))
    counts = info["counts"]
    lines = [
        _stamp(now_ist), "",
        f"Scanned {counts['scanned']} F&O stocks -- data: {info['data_source']}",
        f"Moved beyond +/-{MOMENTUM_PCT:.0f}%: {counts['movers']} | below: {counts['below_move']} | no price: {counts['no_price']}",
        "",
        f"Watchlist ({len(result['listed'])}): {', '.join(result['listed']) or 'none'} -- final list at 9:25 after the Nifty 9:15-9:25 candle",
    ]
    if rejected:
        lines += ["", f"Moved but rejected ({len(rejected)}):"] + [_fmt_rejected(s, r) for s, r in rejected]
    await _notify(ctx, f"FLY OI SCN: {len(result['listed'])} on the 9:20 watchlist", "\n".join(lines))


async def _run_scan_1(ctx, broker, today: date, now_ist: datetime) -> None:
    info = ctx.state["scans"][SCAN_1]
    if info["status"] == "pending":
        info["started_at"] = now_ist.isoformat()
        result = await _scan(ctx, broker, today, now_ist, SCAN_1)
        if result["failed"]:
            info["error"] = result["data_source"]
            return  # retried in 3 seconds
        info.update(status="running", counts=result["counts"], data_source=result["data_source"])
        await _save_rows(ctx, today, SCAN_1, {s: _row_for_db(r) for s, r in result["rows"].items()})
        info["listed"] = result["listed"]
        info["pending"] = {s: _pending_entry(r) for s, r in result["pending"].items()}
        info["rows_summary"] = {s: _summary(r) for s, r in result["rows"].items() if r["passed_move"]}
    else:
        # Retry the movers whose 09:15 candle wasn't out yet.
        still = {}
        for symbol, row in info["pending"].items():
            equity = await ctx.db.get(Instrument, uuid.UUID(row["_setup"]["equity_instrument_id"]))
            if equity is not None and await _judge_candle(ctx, broker, today, equity, row):
                _close_row(row)
                if not row["reasons"]:
                    info["listed"][symbol] = row["_setup"]
                await _update_row(ctx, today, symbol, row["_setup"], **{k: v for k, v in _row_for_db(row).items() if k in ("retrace_pct", "passed_retrace", "reasons", "outcome")})
                info["rows_summary"][symbol] = _summary(row)
            else:
                still[symbol] = row
        info["pending"] = still
    if info["pending"] and now_ist.time() < SECOND_SCAN:
        return
    for symbol, row in info["pending"].items():
        row["reasons"].append("no 9:15-9:20 candle from Kite by 9:25")
        info["rows_summary"][symbol] = _summary(row)
        await _update_row(ctx, today, symbol, row["_setup"], reasons=row["reasons"], outcome="rejected")
    info["pending"] = {}
    info.update(status="done", finished_at=now_ist.isoformat())
    await _finish_scan_1(ctx, today, now_ist, {"listed": info.pop("listed")})


def _summary(row: dict) -> dict:
    return {
        "direction": row.get("direction"), "move_pct": row.get("move_pct"), "oi_change_pct": row.get("oi_change_pct"),
        "reasons": list(row["reasons"]), "passed_move": row["passed_move"],
    }


async def _nifty(ctx, broker, today: date) -> dict | None:
    nifty = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == NIFTY_SYMBOL, Instrument.exchange == "NSE"))
    ).scalars().first()
    if nifty is None:
        return None
    bars = await _candles(broker, nifty, _at(today, MARKET_OPEN), _at(today, dtime(9, 25)))
    first = bars.get(_at(today, MARKET_OPEN).astimezone(timezone.utc))
    second = bars.get(_at(today, dtime(9, 20)).astimezone(timezone.utc))
    if first is None or second is None:
        return None
    return {"bias": nifty_bias(first["open"], second["close"]), "open": first["open"], "close": second["close"]}


async def _run_scan_2(ctx, broker, today: date, now_ist: datetime) -> None:
    info = ctx.state["scans"][SCAN_2]
    setups = ctx.state["setups"]
    if ctx.state["nifty"] is None:
        nifty = await _nifty(ctx, broker, today)
        if nifty is None:
            if now_ist.time() < NIFTY_GIVE_UP:
                return  # retried in 3 seconds
            nifty = {"bias": "unavailable", "open": None, "close": None}
        ctx.state["nifty"] = nifty
    bias = ctx.state["nifty"]["bias"]
    info["started_at"] = info.get("started_at") or now_ist.isoformat()

    # Late movers: the same rules for stocks not on the watchlist.
    result = await _scan(ctx, broker, today, now_ist, SCAN_2)
    if result["failed"]:
        result = {"rows": {}, "listed": {}, "pending": {}, "counts": {}, "data_source": result["data_source"]}
    for symbol, row in result["pending"].items():
        row["reasons"].append("no 9:15-9:20 candle from Kite")
        row["outcome"] = "rejected"
    await _save_rows(ctx, today, SCAN_2, {s: _row_for_db(r) for s, r in result["rows"].items()})
    added = result["listed"]
    for symbol, setup in added.items():
        setups[symbol] = setup

    kept, dropped = [], []
    for symbol, setup in setups.items():
        if setup["status"] != WATCHLIST:
            continue
        allowed = bias != "unavailable" and nifty_allows(setup["direction"], bias)
        await _update_row(ctx, today, symbol, setup, nifty_bias=bias, passed_nifty=allowed)
        if not allowed:
            setup["status"] = DROPPED_NIFTY
            dropped.append(symbol)
            await _update_row(ctx, today, symbol, setup, outcome=DROPPED_NIFTY, reasons=[f"Nifty 9:15-9:25 {bias}: gainers excluded" if bias != "unavailable" else "Nifty 9:15-9:25 candle unavailable"])
            continue
        setup["status"] = WATCHING
        kept.append(symbol)
        await _update_row(ctx, today, symbol, setup, outcome="shortlisted")
    await _mark_levels(ctx, broker, today)
    info.update(status="done", finished_at=now_ist.isoformat(), counts=result["counts"], data_source=result["data_source"])

    nifty = ctx.state["nifty"]
    head = (
        f"Nifty 9:15-9:25: {nifty['open']:.2f} -> {nifty['close']:.2f} ({bias}) -- "
        + ("gainers and losers" if bias == "green" else "losers only")
        if bias != "unavailable" else "Nifty 9:15-9:25 candle unavailable from Kite -- no trades today"
    )
    lines = [_stamp(now_ist), "", head, "", f"Final shortlist ({len(kept)}):"]
    for symbol in kept:
        s = setups[symbol]
        if s["breakout_high"] is None:
            levels = "levels pending"
        elif s["direction"] == "CE":
            levels = f"break above {s['breakout_high']:.2f}"
        else:
            levels = f"break below {s['breakout_low']:.2f}"
        lines.append(f"{symbol} {s['direction']}: {levels}" + (" (added at 9:25)" if symbol in added else "") + (" -- expiry blackout, no trade" if s["blackout"] else ""))
    if dropped:
        lines += ["", f"Dropped by the Nifty filter: {', '.join(dropped)}"]
    late_rejected = [(s, _summary(r)) for s, r in result["rows"].items() if r["passed_move"] and r["reasons"]]
    if late_rejected:
        lines += ["", f"Moved by 9:25 but rejected ({len(late_rejected)}):"] + [_fmt_rejected(s, r) for s, r in late_rejected]
    await _notify(ctx, f"FLY OI SCN: {len(kept)} shortlisted at 9:25", "\n".join(lines))
    for symbol in added:
        if setups[symbol]["status"] == WATCHING:
            await _notify(ctx, f"FLY OI SCN: {symbol} {setups[symbol]['direction']} shortlisted at 9:25", f"{_stamp(now_ist)}\n\n{_fmt_stock(symbol, setups[symbol])}")


async def _mark_levels(ctx, broker, today: date) -> None:
    """Each shortlisted stock's 09:15-09:25 high and low (both candles
    needed); retried on every breakout poll until they're in."""
    for symbol, setup in ctx.state["setups"].items():
        if setup["status"] != WATCHING or setup["breakout_high"] is not None:
            continue
        equity = await ctx.db.get(Instrument, uuid.UUID(setup["equity_instrument_id"]))
        if equity is None:
            continue
        bars = await _candles(broker, equity, _at(today, MARKET_OPEN), _at(today, dtime(9, 25)))
        first = bars.get(_at(today, MARKET_OPEN).astimezone(timezone.utc))
        second = bars.get(_at(today, dtime(9, 20)).astimezone(timezone.utc))
        if first is None or second is None:
            continue
        setup["breakout_high"] = max(first["high"], second["high"])
        setup["breakout_low"] = min(first["low"], second["low"])
        await _update_row(ctx, today, symbol, setup, breakout_high=setup["breakout_high"], breakout_low=setup["breakout_low"])


# ---------------------------------------------------------------------
# Entries (09:25-10:30) and exits
# ---------------------------------------------------------------------
async def _watch_breakouts(ctx, broker, today: date, now_ist: datetime) -> None:
    setups = ctx.state["setups"]
    watching = {s: v for s, v in setups.items() if v["status"] == WATCHING}
    if not watching:
        return
    if now_ist.time() >= BREAKOUT_CUTOFF:
        for symbol, setup in watching.items():
            setup["status"] = NO_TRIGGER
            await _update_row(ctx, today, symbol, setup, outcome=NO_TRIGGER)
        return
    last = ctx.state.get("last_poll_at")
    if last and now_ist - datetime.fromisoformat(last) < BREAKOUT_POLL - timedelta(milliseconds=500):
        return
    ctx.state["last_poll_at"] = now_ist.isoformat()
    await _mark_levels(ctx, broker, today)

    equities = {s: await ctx.db.get(Instrument, uuid.UUID(v["equity_instrument_id"])) for s, v in watching.items()}
    quotes, _ = await kite_quotes(broker, [_key(eq) for eq in equities.values() if eq is not None])
    broken = []
    for symbol, setup in watching.items():
        equity = equities.get(symbol)
        price = (quotes.get(_key(equity)) or {}).get("last_price") if equity is not None else None
        if not price or setup["breakout_high"] is None:
            continue
        price = float(price)
        tick_engine.set_real_price(equity.id, price, "kite_rest")
        if (setup["direction"] == "CE" and price > setup["breakout_high"]) or (setup["direction"] == "PE" and price < setup["breakout_low"]):
            broken.append((abs(price - setup["prev_close"]) / setup["prev_close"], symbol, price))
    # Same poll: the strongest move takes the next free slot.
    for _, symbol, price in sorted(broken, reverse=True):
        await _enter(ctx, broker, today, now_ist, symbol, setups[symbol], price)


async def _enter(ctx, broker, today: date, now_ist: datetime, symbol: str, setup: dict, price: float) -> None:
    level = setup["breakout_high"] if setup["direction"] == "CE" else setup["breakout_low"]
    if setup["blackout"]:
        setup.update(status=BLACKOUT, trigger_time=now_ist.isoformat(), trigger_spot=price)
        await _update_row(ctx, today, symbol, setup, outcome=BLACKOUT)
        return
    if ctx.state["trades_today"] >= MAX_TRADES_PER_DAY:
        setup.update(status=LIMIT_REACHED, trigger_time=now_ist.isoformat(), trigger_spot=price)
        await _update_row(ctx, today, symbol, setup, outcome=LIMIT_REACHED)
        await _notify(ctx, f"FLY OI SCN: {symbol} broke out -- daily limit reached", f"{_stamp(now_ist)}\n\n{symbol} broke {level:.2f} at {price:.2f}, but {MAX_TRADES_PER_DAY} trades are already taken today.")
        return
    option = pick_otm(await _options(ctx, uuid.UUID(setup["equity_instrument_id"]), date.fromisoformat(setup["future_expiry"]), setup["direction"]), setup["direction"], price)
    if option is None:
        return
    premium = await _ltp(broker, option)
    if premium is None:
        return  # no live price for the option yet: next poll
    tick_engine.set_real_price(option.id, premium, "kite_rest")
    await ctx.get_price(option.id)  # keeps the live feed tracking it for the P&L
    lot_size = option.lot_size or DEFAULT_LOT_SIZE
    await ctx.open_leg(option, "buy", float(LOTS_PER_SETUP * lot_size), premium)
    ctx.state["trades_today"] += 1
    setup.update(
        status=TRIGGERED, trigger_time=now_ist.isoformat(), trigger_spot=price, option_instrument_id=str(option.id),
        option_symbol=option.symbol, lot_size=lot_size, entry_premium=premium,
    )
    await _update_row(ctx, today, symbol, setup, outcome=TRIGGERED, option_symbol=option.symbol, entry_at=now_ist.astimezone(timezone.utc), entry_premium=premium)
    await _notify(
        ctx, f"FLY OI SCN: bought {option.symbol}",
        f"{_stamp(now_ist)}\n\n{symbol} broke {level:.2f} at {price:.2f} -> bought {LOTS_PER_SETUP} lot ({lot_size}) of {option.symbol} @ {premium:.2f}"
        f"\nTrade {ctx.state['trades_today']} of {MAX_TRADES_PER_DAY} today. Exit: two 5-min closes {'below' if setup['direction'] == 'CE' else 'above'} the 8-SMA, or 15:10.",
        alert_type=AlertType.ORDER_EXECUTED.value,
    )
    ctx.note("entered", signal=setup["direction"], reason=f"{symbol}: broke {level:.2f}, bought {option.symbol} @ {premium:.2f}")


def _last_closed_boundary(now_ist: datetime) -> datetime:
    """Start of the newest 5-minute candle closed at least CANDLE_SETTLE ago."""
    t = now_ist - CANDLE_SETTLE
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0) - CANDLE


async def _manage_exits(ctx, broker, today: date, now_ist: datetime) -> None:
    open_trades = {s: v for s, v in ctx.state["setups"].items() if v["status"] == TRIGGERED}
    if not open_trades:
        return
    if now_ist.time() >= REPORT_TIME:
        for symbol, setup in open_trades.items():
            await _close(ctx, broker, today, now_ist, symbol, setup, "15:10 IST close")
        return
    if broker is None:
        return
    boundary = _last_closed_boundary(now_ist).isoformat()
    if ctx.state.get("last_exit_check") == boundary:
        return
    ctx.state["last_exit_check"] = boundary
    start = _at(previous_trading_day(today), SMA_HISTORY_FROM)
    for symbol, setup in open_trades.items():
        equity = await ctx.db.get(Instrument, uuid.UUID(setup["equity_instrument_id"]))
        if equity is None:
            continue
        bars = await _candles(broker, equity, start, now_ist)
        closed = [b for ts, b in sorted(bars.items()) if ts + CANDLE <= now_ist.astimezone(timezone.utc)]
        if sma_exit_due(setup["direction"], closed, datetime.fromisoformat(setup["trigger_time"])):
            side = "below" if setup["direction"] == "CE" else "above"
            await _close(ctx, broker, today, now_ist, symbol, setup, f"2 consecutive 5-min closes {side} the 8-SMA")


async def _close(ctx, broker, today: date, now_ist: datetime, symbol: str, setup: dict, reason: str) -> None:
    option = await ctx.db.get(Instrument, uuid.UUID(setup["option_instrument_id"]))
    if option is None:
        logger.error("FLY OI SCN: %s's option %s is gone from the catalog -- can't close it", symbol, setup["option_symbol"])
        return
    exit_premium = await _ltp(broker, option) if broker is not None else None
    if exit_premium is None:
        exit_premium = await ctx.get_price(option.id)
    if exit_premium is None:
        exit_premium = setup["entry_premium"]
    quantity = float(LOTS_PER_SETUP * (setup["lot_size"] or DEFAULT_LOT_SIZE))
    await ctx.close_leg(option, "sell", quantity, exit_premium)
    pnl = (exit_premium - setup["entry_premium"]) * quantity
    pnl_pct = pnl / (setup["entry_premium"] * quantity) * 100.0 if setup["entry_premium"] else 0.0
    status = EXITED if "SMA" in reason else EOD_CLOSED
    setup.update(status=status, exit_time=now_ist.isoformat(), exit_premium=exit_premium, exit_reason=reason, pnl=pnl)
    await ctx.record_trade(
        legs=[{"instrument_id": setup["option_instrument_id"], "side": "long", "quantity": quantity, "entry_price": setup["entry_premium"], "exit_price": exit_premium}],
        pnl=pnl, pnl_pct=pnl_pct, exit_reason=f"{symbol}: {reason}", opened_at=datetime.fromisoformat(setup["trigger_time"]),
        closed_at=now_ist, alert=False,
    )
    await _update_row(ctx, today, symbol, setup, outcome=status, exit_at=now_ist.astimezone(timezone.utc), exit_premium=exit_premium, exit_reason=reason[:100], pnl=pnl)
    await _notify(
        ctx, f"FLY OI SCN: {symbol} {setup['direction']} closed", f"{_stamp(now_ist)}\n\n{setup['option_symbol']}: {setup['entry_premium']:.2f} -> {exit_premium:.2f}\n{reason}: P&L {pnl:+.2f}",
        alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO if pnl >= 0 else AlertSeverity.WARNING,
    )
    ctx.note("exited", signal="SELL", reason=f"{symbol}: {reason}, P&L {pnl:+.2f}")


async def _report(ctx, now_ist: datetime) -> None:
    setups = ctx.state["setups"]
    closed = [s for s in setups.values() if s["status"] in (EXITED, EOD_CLOSED)]
    wins = [s for s in closed if (s["pnl"] or 0) > 0]
    losses = [s for s in closed if (s["pnl"] or 0) < 0]
    total = sum(s["pnl"] or 0 for s in closed)
    win_rate = len(wins) / (len(wins) + len(losses)) * 100.0 if wins or losses else 0.0
    shortlisted = [s for s in setups.values() if s["status"] not in (WATCHLIST, DROPPED_NIFTY)]
    summary = (
        f"Watchlist {len(setups)} | Shortlisted {len(shortlisted)} | Traded {len(closed)} | "
        f"Win rate {win_rate:.0f}% ({len(wins)}W/{len(losses)}L) | Total P&L {total:+.2f}"
    )
    rows = "\n".join(
        f"{sym}: {s['status']}" + (f" P&L {s['pnl']:+.2f}" if s.get("pnl") is not None else "") for sym, s in setups.items()
    ) or "(none)"
    await _notify(ctx, "FLY OI SCN: 15:10 report", f"{_stamp(now_ist)}\n\n{summary}\n\n{rows}")
    ctx.state["report_sent"] = True
    ctx.note("exited", reason=summary)


# ---------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------
def _next_wake(state: dict, now_ist: datetime) -> datetime | None:
    today = now_ist.date()

    def due(t: dtime) -> datetime:
        target = _at(today, t)
        return target if now_ist < target else now_ist + RETRY

    wakes = []
    if not state["warmed"] and now_ist < _at(today, FIRST_SCAN):
        wakes.append(due(WARM_UP))
    if state["scans"][SCAN_1]["status"] != "done":
        wakes.append(due(FIRST_SCAN))
    elif state["scans"][SCAN_2]["status"] != "done":
        wakes.append(due(SECOND_SCAN))
    statuses = {s["status"] for s in state["setups"].values()}
    if state["scans"][SCAN_2]["status"] == "done" and WATCHING in statuses and now_ist.time() < BREAKOUT_CUTOFF:
        wakes.append(now_ist + BREAKOUT_POLL)
    if TRIGGERED in statuses:
        wakes.append(_last_closed_boundary(now_ist) + 2 * CANDLE + CANDLE_SETTLE)
    if not state["report_sent"]:
        wakes.append(due(REPORT_TIME))
    return min(wakes) if wakes else None


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------
async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    today = now_ist.date()
    if ctx.state.get("session_date") != today.isoformat() or ctx.state.get("version") != VERSION:
        ctx.state.clear()
        ctx.state.update(_fresh_state(today))

    broker = None if getattr(ctx, "is_backtest", False) else await _live_broker(ctx)
    if ctx.state.pop("force_exit", False):
        for symbol, setup in ctx.state["setups"].items():
            if setup["status"] == TRIGGERED:
                await _close(ctx, broker, today, now_ist, symbol, setup, "manual")
        ctx.note("exited", reason="manual exit -- open trades closed")
        return
    if getattr(ctx, "is_backtest", False):
        ctx.note("skipped", reason="FLY OI SCN v6 runs on live Kite data; a historical replay isn't supported")
        return
    if not is_trading_day(today) or now_ist.time() < MARKET_OPEN:
        ctx.note("skipped", reason="market closed")
        return

    try:
        await _step(ctx, broker, today, now_ist)
    finally:
        wake = _next_wake(ctx.state, now_ist)
        if wake is not None:
            ctx.wake_at(wake)


async def _skip_day(ctx, now_ist: datetime, reason: str) -> None:
    """The 09:20 scan couldn't run in time: the rules measure the move at
    09:20, so a scan run later would pick the wrong stocks. No trades today."""
    for info in ctx.state["scans"].values():
        info.update(status="done", skipped=reason)
    await _notify(ctx, "FLY OI SCN: no scan today", f"{_stamp(now_ist)}\n\n{reason} -- the 9:20 scan can't run late, so no trades today.", severity=AlertSeverity.WARNING)


async def _step(ctx, broker, today: date, now_ist: datetime) -> None:
    t = now_ist.time()
    scans = ctx.state["scans"]
    if scans[SCAN_1]["status"] == "pending" and t >= SECOND_SCAN:
        error = scans[SCAN_1].get("error")
        await _skip_day(ctx, now_ist, f"Kite quotes weren't available by 9:25 ({error})" if broker is not None and error else "Zerodha wasn't logged in by 9:25")
    if broker is None:
        if FIRST_SCAN <= t < SECOND_SCAN and not ctx.state["login_alert_sent"] and scans[SCAN_1]["status"] != "done":
            ctx.state["login_alert_sent"] = True
            await _notify(ctx, "FLY OI SCN: waiting for Zerodha login", f"{_stamp(now_ist)}\n\nThe 9:20 scan needs live Kite data -- it runs as soon as Zerodha is logged in.", severity=AlertSeverity.WARNING)
        await _manage_exits(ctx, None, today, now_ist)
        if t >= REPORT_TIME and not ctx.state["report_sent"]:
            await _report(ctx, now_ist)
        ctx.note("hold", reason="waiting for Zerodha login")
        return

    if not ctx.state["warmed"] and t >= WARM_UP:
        ctx.state["warmed"] = True
        try:
            await broker.get_instruments("NSE")
        except KiteAPIError as exc:
            logger.warning("FLY OI SCN: couldn't preload Kite's NSE instrument list: %s", exc)
    if scans[SCAN_1]["status"] != "done" and t >= FIRST_SCAN:
        await _run_scan_1(ctx, broker, today, now_ist)
    if scans[SCAN_1]["status"] == "done" and scans[SCAN_2]["status"] != "done" and t >= SECOND_SCAN:
        await _run_scan_2(ctx, broker, today, now_ist)
    if scans[SCAN_2]["status"] == "done":
        await _watch_breakouts(ctx, broker, today, now_ist)
    await _manage_exits(ctx, broker, today, now_ist)
    if t >= REPORT_TIME and not ctx.state["report_sent"]:
        await _report(ctx, now_ist)
        return
    statuses = [s["status"] for s in ctx.state["setups"].values()]
    ctx.note("hold", reason=f"{len(statuses)} on the list, {statuses.count(WATCHING)} waiting for a breakout, {statuses.count(TRIGGERED)} open")
