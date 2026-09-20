"""
F&O Opening-Candle Momentum Scanner (native, unsandboxed)
===========================================================

Ported from the standalone "FLY OI SCN" paper-trading scanner (see that
project's PRD.md) into TradingMaster's native-strategy runner. Same
Steps 1-12, now scanning every stock in the platform's own F&O futures
catalog (Instrument rows with instrument_type == "future") instead of a
hardcoded list, so the universe stays in sync with whatever's been
backfilled.

  Step 1  Universe:     every stock with an active NFO stock-futures
                         contract in the catalog (excludes index futures,
                         which have no underlying_instrument_id equity).
  Step 2  9:20 scan:     |% move vs previous close| > 2%.
  Step 3  OI confirm:    stock-futures OI % change > 7% -- computed from
                         the near-month future's own 5m OI series: the
                         last 5m candle of the previous trading session
                         vs the latest 5m candle available now (an
                         intraday-rolling reading of an EOD-to-EOD
                         comparison, per the original PRD's own
                         "Open Assumptions" -- either interpretation was
                         left unresolved there).
  Step 4  Opening cand.: Open == Low (CE) / Open == High (PE) within
                         0.05%, from the 9:15-9:20 5m candle.
  Step 5  Retracement:   reject if the candle's own close has retraced
                         >= 50% of (High-Low) from the defining extreme.
  Step 6  Nifty filter:  green Nifty 9:15-9:20 candle keeps gainers+
                         losers; red Nifty keeps losers only.
  Step 7  Breakout marks: each shortlisted stock's 9:15-9:25 High/Low.
  Step 8  Entry:          break above/below the 9:25 level -> buy ~2%
                         OTM Call/Put, current-month expiry.
  Step 9  Trigger cutoff: no breakout by 10:30 -> "no_trigger".
  Step 10 Exit:           8-SMA (5m closes) exit -- 2 consecutive closes
                         below (CE) / above (PE) the SMA; force-closed at
                         3:10 PM regardless.
  Step 11 Expiry blackout: no entry within 2 trading days either side of
                         the stock's current-month expiry.
  Step 12 Sizing:         fixed 1 lot per triggered setup.

State (`ctx.state`) is a flat JSON-safe dict keyed by IST session date --
switching to a new trading day resets everything. Each shortlisted
stock's own sub-state lives under state["setups"][symbol], deliberately
a plain dict (not a dataclass) for the same JSON-persistence reason
nifty_pcr_credit_spread.py's `position` is.

Runs through services/paper_trading/native_runner.py; see that module's
docstring for why "native" strategies run trusted/unsandboxed. The
9:20 shortlist and 3:10 report are pushed via the platform's own Alert
system (app.services.alerts.service.create_alert) rather than
ctx.record_trade's spread-oriented alert, since this is a single-leg
long option buy, not a 2-leg spread.
"""

import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import select

from app.models.alert import AlertSeverity, AlertType
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import PaperNativeTrade
from app.services.alerts.service import create_alert
from app.services.notifications.telegram import send_telegram
from app.services.broker.zerodha_broker import IST

# ---------------------------------------------------------------------
# Thresholds & timings (PRD.md / scanner/config.py in the source project)
# ---------------------------------------------------------------------
MOMENTUM_PCT = 2.0
OI_CHANGE_PCT = 7.0
OPEN_EQ_TOLERANCE_PCT = 0.05
MAX_RETRACEMENT_PCT = 50.0
OTM_PCT = 2.0
SMA_PERIOD = 8
SMA_CONFIRM = 2
EXPIRY_BLACKOUT_DAYS = 2
LOTS_PER_SETUP = 1
DEFAULT_LOT_SIZE = 1

MARKET_OPEN = dtime(9, 15)
OPENING_CANDLE_END = dtime(9, 20)
BREAKOUT_LEVEL_TIME = dtime(9, 25)
BREAKOUT_CUTOFF = dtime(10, 30)
REPORT_TIME = dtime(15, 10)
CANDLE_TIMEFRAME = "5m"


# ---------------------------------------------------------------------
# Pure filter math (ported verbatim from scanner/filters.py & positions.py)
# ---------------------------------------------------------------------
def momentum_direction(pct_change: float) -> str:
    return "CE" if pct_change > 0 else "PE"


def passes_momentum(pct_change: float) -> bool:
    return abs(pct_change) > MOMENTUM_PCT


def passes_oi_change(oi_pct_change: float | None) -> bool:
    if oi_pct_change is None:
        return False
    return abs(oi_pct_change) > OI_CHANGE_PCT


def opening_strength_ok(o: float, h: float, l: float, direction: str) -> bool:
    if o == 0:
        return False
    reference = l if direction == "CE" else h
    return abs(o - reference) / o * 100.0 <= OPEN_EQ_TOLERANCE_PCT


def retracement_pct(h: float, l: float, c: float, direction: str) -> float:
    candle_range = h - l
    if candle_range <= 0:
        return 0.0
    if direction == "CE":
        return (h - c) / candle_range * 100.0
    return (c - l) / candle_range * 100.0


def passes_retracement(h: float, l: float, c: float, direction: str) -> bool:
    return retracement_pct(h, l, c, direction) < MAX_RETRACEMENT_PCT


def nifty_filter_allows(direction: str, nifty_open: float, nifty_close: float) -> bool:
    nifty_green = nifty_close > nifty_open
    return nifty_green or direction == "PE"


def sma_series(closes: list[float], period: int = SMA_PERIOD) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(closes)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(closes[i + 1 - period : i + 1]) / period)
    return out


def sma_exit_triggered(direction: str, closes: list[float], period: int = SMA_PERIOD, confirm: int = SMA_CONFIRM) -> bool:
    sma = sma_series(closes, period)
    if len(sma) < confirm or any(v is None for v in sma[-confirm:]):
        return False
    recent_closes, recent_sma = closes[-confirm:], sma[-confirm:]
    if direction == "CE":
        return all(c < s for c, s in zip(recent_closes, recent_sma))
    return all(c > s for c, s in zip(recent_closes, recent_sma))


def in_expiry_blackout(as_of: date, expiry: date, blackout_days: int = EXPIRY_BLACKOUT_DAYS) -> bool:
    """Trading-day-aware (weekends only, exchange holidays not modeled --
    same approximation scanner/kite_client.py's pd.bdate_range made)."""
    if as_of == expiry:
        return True
    step = timedelta(days=1) if as_of < expiry else timedelta(days=-1)
    d, trading_days = as_of, 0
    while d != expiry:
        d += step
        if d.weekday() < 5:
            trading_days += 1
    return trading_days <= blackout_days


# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------
def _ist_to_utc(d: date, t: dtime) -> datetime:
    return datetime.combine(d, t, tzinfo=IST).astimezone(timezone.utc)


async def _candle_range(ctx, instrument_id: uuid.UUID, start_ist: datetime, end_ist: datetime) -> dict | None:
    """Aggregates every stored 5m bar in [start, end) into one OHLC
    reading, matching scanner/pipeline.py's `_candle_from_df` (which did
    the same over possibly-multi-bar Kite historical responses)."""
    rows = (
        await ctx.db.execute(
            select(OhlcvCandle)
            .where(
                OhlcvCandle.instrument_id == instrument_id,
                OhlcvCandle.timeframe == CANDLE_TIMEFRAME,
                OhlcvCandle.ts >= start_ist.astimezone(timezone.utc),
                OhlcvCandle.ts < end_ist.astimezone(timezone.utc),
            )
            .order_by(OhlcvCandle.ts)
        )
    ).scalars().all()
    if not rows:
        return None
    return {
        "open": rows[0].open, "high": max(r.high for r in rows), "low": min(r.low for r in rows),
        "close": rows[-1].close,
    }


async def _prev_close(ctx, instrument_id: uuid.UUID, today: date) -> float | None:
    row = (
        await ctx.db.execute(
            select(OhlcvCandle.close)
            .where(
                OhlcvCandle.instrument_id == instrument_id,
                OhlcvCandle.timeframe == CANDLE_TIMEFRAME,
                OhlcvCandle.ts < _ist_to_utc(today, MARKET_OPEN),
            )
            .order_by(OhlcvCandle.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def _futures_oi_pct_change(ctx, future_instrument_id: uuid.UUID, today: date) -> float | None:
    prev_oi = (
        await ctx.db.execute(
            select(OhlcvCandle.open_interest)
            .where(
                OhlcvCandle.instrument_id == future_instrument_id,
                OhlcvCandle.timeframe == CANDLE_TIMEFRAME,
                OhlcvCandle.ts < _ist_to_utc(today, MARKET_OPEN),
                OhlcvCandle.open_interest.is_not(None),
            )
            .order_by(OhlcvCandle.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    latest_oi = (
        await ctx.db.execute(
            select(OhlcvCandle.open_interest)
            .where(
                OhlcvCandle.instrument_id == future_instrument_id,
                OhlcvCandle.timeframe == CANDLE_TIMEFRAME,
                OhlcvCandle.open_interest.is_not(None),
            )
            .order_by(OhlcvCandle.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not prev_oi or latest_oi is None:
        return None
    return (latest_oi - prev_oi) / prev_oi * 100.0


async def _nearest_future(ctx, equity_id: uuid.UUID, today: date) -> Instrument | None:
    return (
        await ctx.db.execute(
            select(Instrument)
            .where(
                Instrument.instrument_type == "future", Instrument.underlying_instrument_id == equity_id,
                Instrument.expiry.is_not(None), Instrument.expiry >= today,
            )
            .order_by(Instrument.expiry)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _pick_otm_option(ctx, equity_id: uuid.UUID, expiry: date, direction: str, spot: float) -> Instrument | None:
    target = spot * (1 + OTM_PCT / 100.0) if direction == "CE" else spot * (1 - OTM_PCT / 100.0)
    options = (
        await ctx.db.execute(
            select(Instrument).where(
                Instrument.instrument_type == "option", Instrument.underlying_instrument_id == equity_id,
                Instrument.expiry == expiry, Instrument.option_type == direction, Instrument.strike.is_not(None),
            )
        )
    ).scalars().all()
    if not options:
        return None
    return min(options, key=lambda o: abs(o.strike - target))


# ---------------------------------------------------------------------
# Phase A: the 9:20 scan (Steps 1-6)
# ---------------------------------------------------------------------
async def _run_scan(ctx, today: date) -> tuple[dict, list[dict]]:
    """Returns (setups keyed by symbol, alert rows for every stock that
    passed -- both pass and fail get evaluated, only passes shortlisted,
    matching pipeline.run_scan / shortlist)."""
    nifty = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == "NIFTY 50"))
    ).scalar_one_or_none()
    nifty_candle = (
        await _candle_range(ctx, nifty.id, datetime.combine(today, MARKET_OPEN, tzinfo=IST), datetime.combine(today, OPENING_CANDLE_END, tzinfo=IST))
        if nifty else None
    )
    if nifty_candle is None:
        ctx.note("error", reason="no Nifty 9:15-9:20 candle available -- cannot run the scan")
        return {}, []

    futures = (
        await ctx.db.execute(
            select(Instrument).where(Instrument.instrument_type == "future", Instrument.underlying_instrument_id.is_not(None))
        )
    ).scalars().all()

    setups: dict = {}
    shortlisted_rows: list[dict] = []
    for fut in futures:
        equity = await ctx.db.get(Instrument, fut.underlying_instrument_id)
        if equity is None:
            continue
        symbol = equity.symbol

        price = await ctx.get_price(equity.id)
        prev_close = await _prev_close(ctx, equity.id, today)
        if price is None or not prev_close:
            continue
        pct_change = (price - prev_close) / prev_close * 100.0
        if not passes_momentum(pct_change):
            continue

        direction = momentum_direction(pct_change)
        opening = await _candle_range(ctx, equity.id, datetime.combine(today, MARKET_OPEN, tzinfo=IST), datetime.combine(today, OPENING_CANDLE_END, tzinfo=IST))
        if opening is None:
            continue

        near_future = await _nearest_future(ctx, equity.id, today)
        oi_pct_change = await _futures_oi_pct_change(ctx, near_future.id, today) if near_future else None

        reasons = []
        if not passes_oi_change(oi_pct_change):
            reasons.append("OI change below threshold")
        if not opening_strength_ok(opening["open"], opening["high"], opening["low"], direction):
            reasons.append("Open != Low/High beyond tolerance")
        if not passes_retracement(opening["high"], opening["low"], opening["close"], direction):
            reasons.append("retraced >= 50%")
        if not nifty_filter_allows(direction, nifty_candle["open"], nifty_candle["close"]):
            reasons.append("Nifty red -- gainers excluded")

        if reasons:
            continue

        setups[symbol] = {
            "equity_instrument_id": str(equity.id),
            "direction": direction,
            "status": "watching",
            "pct_change": pct_change,
            "oi_pct_change": oi_pct_change,
            "breakout_high": None,
            "breakout_low": None,
            "underlying_closes": [],
            "last_candle_ts": None,
            "trigger_time": None,
            "trigger_spot": None,
            "option_instrument_id": None,
            "option_symbol": None,
            "lot_size": None,
            "entry_premium": None,
            "exit_time": None,
            "exit_premium": None,
            "exit_reason": None,
            "pnl": None,
        }
        shortlisted_rows.append({"symbol": symbol, "direction": direction, "pct_change": pct_change, "oi_pct_change": oi_pct_change})

    return setups, shortlisted_rows


async def _mark_breakout_levels(ctx, setups: dict, today: date) -> None:
    for symbol, setup in setups.items():
        equity_id = uuid.UUID(setup["equity_instrument_id"])
        rng = await _candle_range(ctx, equity_id, datetime.combine(today, MARKET_OPEN, tzinfo=IST), datetime.combine(today, BREAKOUT_LEVEL_TIME, tzinfo=IST))
        if rng is None:
            continue
        setup["breakout_high"] = rng["high"]
        setup["breakout_low"] = rng["low"]


# ---------------------------------------------------------------------
# Phase B/C: entry, SMA exit, EOD close (Steps 7-11)
# ---------------------------------------------------------------------
async def _try_enter(ctx, symbol: str, setup: dict, now_ist: datetime) -> None:
    if setup["status"] != "watching" or setup["breakout_high"] is None:
        return
    if now_ist.time() >= BREAKOUT_CUTOFF:
        setup["status"] = "no_trigger"
        return

    equity_id = uuid.UUID(setup["equity_instrument_id"])
    price = await ctx.get_price(equity_id)
    if price is None:
        return
    direction = setup["direction"]
    breakout_hit = price > setup["breakout_high"] if direction == "CE" else price < setup["breakout_low"]
    if not breakout_hit:
        return

    equity = await ctx.db.get(Instrument, equity_id)
    near_future = await _nearest_future(ctx, equity_id, now_ist.date())
    expiry = near_future.expiry if near_future else None
    if expiry is None or in_expiry_blackout(now_ist.date(), expiry):
        setup["status"] = "blackout"
        return

    option = await _pick_otm_option(ctx, equity_id, expiry, direction, price)
    if option is None:
        return
    premium = await ctx.get_price(option.id)
    if premium is None:
        return

    lot_size = option.lot_size or DEFAULT_LOT_SIZE
    await ctx.open_leg(option, "buy", float(LOTS_PER_SETUP * lot_size), premium)

    setup.update(
        status="triggered", trigger_time=now_ist.isoformat(), trigger_spot=price,
        option_instrument_id=str(option.id), option_symbol=option.symbol, lot_size=lot_size, entry_premium=premium,
    )
    ctx.note("entered", signal=direction, reason=f"{symbol}: broke {setup['breakout_high'] if direction == 'CE' else setup['breakout_low']:.2f}, bought {option.symbol} @ {premium:.2f}")


async def _close_position(ctx, symbol: str, setup: dict, now_ist: datetime, exit_reason: str) -> None:
    option_id = uuid.UUID(setup["option_instrument_id"])
    option = await ctx.db.get(Instrument, option_id)
    exit_premium = await ctx.get_price(option_id)
    if exit_premium is None:
        exit_premium = setup["entry_premium"]

    lot_size = setup["lot_size"] or DEFAULT_LOT_SIZE
    quantity = float(LOTS_PER_SETUP * lot_size)
    await ctx.close_leg(option, "sell", quantity, exit_premium)

    pnl = (exit_premium - setup["entry_premium"]) * quantity
    setup.update(status="exited" if "SMA" in exit_reason else "eod_closed", exit_time=now_ist.isoformat(), exit_premium=exit_premium, exit_reason=exit_reason, pnl=pnl)

    ctx.db.add(
        PaperNativeTrade(
            deployment_id=ctx.deployment.id,
            opened_at=datetime.fromisoformat(setup["trigger_time"]),
            closed_at=now_ist.astimezone(timezone.utc),
            legs=[{
                "instrument_id": str(option_id), "side": "long", "quantity": quantity,
                "entry_price": setup["entry_premium"], "exit_price": exit_premium,
            }],
            pnl=pnl, pnl_pct=(pnl / (setup["entry_premium"] * quantity) * 100.0) if setup["entry_premium"] else 0.0,
            exit_reason=exit_reason,
        )
    )
    alert_title = f"{symbol} {setup['direction']} closed"
    alert_message = f"{exit_reason}: P&L {pnl:+.2f}"
    await create_alert(
        ctx.db, user_id=ctx.portfolio.user_id, alert_type=AlertType.ORDER_EXECUTED.value,
        severity=AlertSeverity.INFO if pnl >= 0 else AlertSeverity.WARNING,
        title=alert_title, message=alert_message,
        object_type="paper_native_deployment", object_id=str(ctx.deployment.id),
    )
    await send_telegram(alert_title, alert_message)
    ctx.note("exited", signal="SELL", reason=f"{symbol}: {exit_reason}, P&L {pnl:+.2f}")


async def _manage_position(ctx, symbol: str, setup: dict, now_ist: datetime, today: date) -> None:
    if setup["status"] != "triggered":
        return

    equity_id = uuid.UUID(setup["equity_instrument_id"])
    last_ts = datetime.fromisoformat(setup["last_candle_ts"]) if setup["last_candle_ts"] else datetime.fromisoformat(setup["trigger_time"])
    new_rows = (
        await ctx.db.execute(
            select(OhlcvCandle)
            .where(OhlcvCandle.instrument_id == equity_id, OhlcvCandle.timeframe == CANDLE_TIMEFRAME, OhlcvCandle.ts > last_ts.astimezone(timezone.utc))
            .order_by(OhlcvCandle.ts)
        )
    ).scalars().all()
    if new_rows:
        setup["underlying_closes"].extend(r.close for r in new_rows)
        setup["last_candle_ts"] = new_rows[-1].ts.isoformat()

    if now_ist.time() >= REPORT_TIME:
        await _close_position(ctx, symbol, setup, now_ist, "3:10pm IST cutoff (marked-to-market)")
        return

    direction = setup["direction"]
    if sma_exit_triggered(direction, setup["underlying_closes"]):
        side = "below" if direction == "CE" else "above"
        await _close_position(ctx, symbol, setup, now_ist, f"2 consecutive closes {side} 8-SMA")


# ---------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------
async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    today = now_ist.date()

    if ctx.state.get("session_date") != today.isoformat():
        ctx.state.clear()
        ctx.state.update(session_date=today.isoformat(), shortlist_done=False, breakout_marked=False, report_sent=False, setups={})

    if ctx.state.pop("force_exit", False):
        for symbol, setup in ctx.state["setups"].items():
            if setup["status"] == "triggered":
                await _close_position(ctx, symbol, setup, now_ist, "manual")
        ctx.note("exited", reason="manual exit -- all open positions closed")
        return

    if now_ist.time() < MARKET_OPEN:
        ctx.note("skipped", reason="before market open")
        return

    if not ctx.state["shortlist_done"] and now_ist.time() >= OPENING_CANDLE_END:
        setups, shortlisted_rows = await _run_scan(ctx, today)
        ctx.state["setups"] = setups
        ctx.state["shortlist_done"] = True
        lines = "\n".join(f"{r['symbol']} {r['direction']} {r['pct_change']:+.2f}% OI{r['oi_pct_change']:+.1f}%" for r in shortlisted_rows) or "(none)"
        alert_title = f"F&O Opening Momentum: {len(shortlisted_rows)} shortlisted"
        await create_alert(
            ctx.db, user_id=ctx.portfolio.user_id, alert_type=AlertType.STRATEGY_SIGNAL.value, severity=AlertSeverity.INFO,
            title=alert_title, message=lines, object_type="paper_native_deployment", object_id=str(ctx.deployment.id),
        )
        await send_telegram(alert_title, lines)
        ctx.note("entered" if shortlisted_rows else "skipped", reason=f"9:20 scan: {len(shortlisted_rows)} shortlisted")
        return

    if ctx.state["shortlist_done"] and not ctx.state["breakout_marked"] and now_ist.time() >= BREAKOUT_LEVEL_TIME:
        await _mark_breakout_levels(ctx, ctx.state["setups"], today)
        ctx.state["breakout_marked"] = True

    if ctx.state["breakout_marked"]:
        for symbol, setup in ctx.state["setups"].items():
            await _try_enter(ctx, symbol, setup, now_ist)
            await _manage_position(ctx, symbol, setup, now_ist, today)

    if now_ist.time() >= REPORT_TIME and not ctx.state["report_sent"]:
        setups = ctx.state["setups"]
        triggered = [s for s in setups.values() if s["status"] in ("exited", "eod_closed")]
        wins = [s for s in triggered if (s["pnl"] or 0) > 0]
        losses = [s for s in triggered if (s["pnl"] or 0) < 0]
        total_pnl = sum(s["pnl"] or 0 for s in triggered)
        win_rate = (len(wins) / (len(wins) + len(losses)) * 100.0) if (wins or losses) else 0.0
        summary = (
            f"Shortlisted {len(setups)} | Triggered {len(triggered)} | "
            f"Win rate {win_rate:.0f}% ({len(wins)}W/{len(losses)}L) | Total P&L {total_pnl:+.2f}"
        )
        rows = "\n".join(
            f"{sym}: {'triggered' if s['status'] in ('exited','eod_closed') else s['status']}"
            + (f" P&L {s['pnl']:+.2f}" if s.get("pnl") is not None else "")
            for sym, s in setups.items()
        ) or "(none)"
        report_message = f"{summary}\n\n{rows}"
        await create_alert(
            ctx.db, user_id=ctx.portfolio.user_id, alert_type=AlertType.STRATEGY_SIGNAL.value, severity=AlertSeverity.INFO,
            title="F&O Opening Momentum: 3:10pm report", message=report_message,
            object_type="paper_native_deployment", object_id=str(ctx.deployment.id),
        )
        await send_telegram("F&O Opening Momentum: 3:10pm report", report_message)
        ctx.state["report_sent"] = True
        ctx.note("exited", reason=summary)
        return

    ctx.note("hold", reason=f"{len(ctx.state.get('setups', {}))} setups tracked")
