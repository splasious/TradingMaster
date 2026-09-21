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
  Step 2  9:20 + 9:25 scan: |% move vs previous close| > 2%, checked
                         twice -- once at 9:20, and again at 9:25 per
                         instruction, to catch any stock that crosses the
                         threshold a few minutes later than the first
                         pass. The 9:25 pass only ever ADDS newly-
                         qualifying stocks to the shortlist; nothing
                         already shortlisted at 9:20 is re-validated or
                         removed even if its numbers have since slipped
                         back below threshold.
  Step 3  OI confirm:    Total OI % change > 7%, where Total OI = the
                         near-month future's own OI + every CE strike's
                         OI + every PE strike's OI for that SAME current
                         running-month expiry (cross-checked against
                         NSE's own OI Spurts methodology, per
                         instruction -- this replaced a futures-OI-only
                         reading, which is still shown broken out in the
                         alert for reference, just no longer the gate on
                         its own). Yesterday's EOD ("1d" candle) reading
                         vs the latest available now, for every leg.
                         Re-checked on the 9:25 pass the same way as
                         Step 2.
  Step 4  Removed -- previously required Open == Low (CE) / Open == High
                         (PE) within 0.05% on the 9:15-9:20 5m candle;
                         dropped per instruction. The 9:15-9:20 candle
                         itself is still fetched and used by Step 5 below.
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

Additionally, informational only (never used to filter/reject a setup --
Step 8's actual entry logic in _try_enter is unchanged, always re-picking
the strike fresh at real breakout time):
  - Each shortlisted stock gets the near-month expiry's Call and Put
    strikes with the most open interest within MAX_OI_STRIKE_BAND_PCT of
    the current spot ("highest OI strike, same stock"), surfaced in the
    shortlist alert as context on where OI is concentrated near the
    breakout.
  - Each shortlisted stock also gets a preview of the ~2% OTM strike
    Step 8's trade setup would buy if evaluated right now (same
    _pick_otm_option logic Step 8 itself uses) -- a preview only, since
    the underlying can keep moving between the scan and the real
    breakout, at which point _try_enter re-picks the strike against
    whatever price is current then.

Every alert/Telegram message this module sends (shortlist at 9:20 and
9:25, exit, 3:10pm report) leads with an explicit "As of <IST timestamp>"
line, and each shortlisted row spells out both the underlying's actual
cash (spot) price move (previous close, current price, resulting %) and
the full Total OI breakdown (futures/CE/PE, yesterday vs today) --
rather than just the computed percentages -- so Step 2's >2% momentum
condition and Step 3's >7% Total OI condition can both be verified
against the raw numbers by eye.

The 9:20 and 9:25 shortlist alerts are sent one-per-stock (_send_shortlist_alert),
not bundled into a single combined message -- per instruction, so each
stock gets its own physically separate Telegram notification instead of
one message listing several stocks together. A 9:20 scan that shortlists
nothing still sends one "0 shortlisted" confirmation so a quiet scan
reads as "ran, found nothing," not silence indistinguishable from the
scan never running; the 9:25 pass has no such fallback (silence there
just means nothing new qualified, consistent with it never sending
anything when nothing was added).

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
MAX_RETRACEMENT_PCT = 50.0
OTM_PCT = 2.0
MAX_OI_STRIKE_BAND_PCT = 10.0  # informational max-OI-near-spot lookup, see module docstring
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


async def _prev_day_oi(ctx, instrument_id: uuid.UUID, today: date) -> float | None:
    """Yesterday's EOD open interest for one contract -- its own daily
    ("1d") candle OI, the actual EOD reading, not a 5m/15m bar that merely
    happens to fall before today's open (which an intraday series can't
    guarantee is genuinely the session's last real print, e.g. across a
    gap in the ticker's own coverage)."""
    return (
        await ctx.db.execute(
            select(OhlcvCandle.open_interest)
            .where(
                OhlcvCandle.instrument_id == instrument_id,
                OhlcvCandle.timeframe == "1d",
                OhlcvCandle.ts < _ist_to_utc(today, MARKET_OPEN),
                OhlcvCandle.open_interest.is_not(None),
            )
            .order_by(OhlcvCandle.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _sum_options_oi(ctx, equity_id: uuid.UUID, expiry: date, option_type: str, today: date) -> tuple[float, float]:
    """(yesterday_total, today_total) OI summed across every strike of one
    option_type ("CE" or "PE") for the given (current running month)
    expiry -- a strike with no OI on file yet on either side contributes
    0, rather than being excluded, so one illiquid strike can't silently
    understate the whole side's total."""
    options = (
        await ctx.db.execute(
            select(Instrument).where(
                Instrument.instrument_type == "option", Instrument.underlying_instrument_id == equity_id,
                Instrument.expiry == expiry, Instrument.option_type == option_type, Instrument.strike.is_not(None),
            )
        )
    ).scalars().all()
    prev_total = 0.0
    latest_total = 0.0
    for option in options:
        prev_total += (await _prev_day_oi(ctx, option.id, today)) or 0.0
        latest_total += (await _latest_oi(ctx, option.id)) or 0.0
    return prev_total, latest_total


async def _total_oi_pct_change(ctx, equity_id: uuid.UUID, future_instrument_id: uuid.UUID, expiry: date, today: date) -> dict | None:
    """Total OI = the near-month future's own OI + every CE strike's OI +
    every PE strike's OI, all for that SAME current-running-month expiry --
    per instruction (cross-checked against NSE's own OI Spurts
    methodology), yesterday's EOD reading vs today's latest, exactly the
    same EOD-to-now convention the futures-only reading used before this
    replaced it as Step 3's actual >7% gate. Returns None if the
    yesterday total is unavailable or zero (nothing to compare against);
    the full per-leg breakdown is returned alongside the total so the
    notification can show its components, not just the combined number."""
    fut_prev = await _prev_day_oi(ctx, future_instrument_id, today)
    fut_latest = await _latest_oi(ctx, future_instrument_id)
    ce_prev, ce_latest = await _sum_options_oi(ctx, equity_id, expiry, "CE", today)
    pe_prev, pe_latest = await _sum_options_oi(ctx, equity_id, expiry, "PE", today)

    prev_total = (fut_prev or 0.0) + ce_prev + pe_prev
    latest_total = (fut_latest or 0.0) + ce_latest + pe_latest
    if not prev_total:
        return None
    return {
        "pct_change": (latest_total - prev_total) / prev_total * 100.0,
        "prev_total": prev_total, "latest_total": latest_total,
        "fut_prev": fut_prev or 0.0, "fut_latest": fut_latest or 0.0,
        "ce_prev": ce_prev, "ce_latest": ce_latest,
        "pe_prev": pe_prev, "pe_latest": pe_latest,
    }


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


async def _latest_oi(ctx, instrument_id: uuid.UUID) -> float | None:
    """Most recent open_interest on file for one option contract, at
    whatever timeframe last carried it -- mirrors NativeContext.get_price's
    own "any timeframe, latest wins" convention rather than assuming a
    specific one, since which timeframe actually gets OI written to it
    depends on which scheduler/backfill last touched this contract."""
    return (
        await ctx.db.execute(
            select(OhlcvCandle.open_interest)
            .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.open_interest.is_not(None))
            .order_by(OhlcvCandle.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _max_oi_strike_near_spot(ctx, equity_id: uuid.UUID, expiry: date, spot: float, option_type: str) -> tuple[float, float] | None:
    """The strike within MAX_OI_STRIKE_BAND_PCT of spot carrying the most
    open interest, for one option_type ("CE" or "PE") -- (strike, oi) or
    None if no strike in that band has any OI on file. Informational only,
    see module docstring: never filters a setup or influences which
    strike Step 8 actually buys."""
    lo, hi = spot * (1 - MAX_OI_STRIKE_BAND_PCT / 100.0), spot * (1 + MAX_OI_STRIKE_BAND_PCT / 100.0)
    options = (
        await ctx.db.execute(
            select(Instrument).where(
                Instrument.instrument_type == "option", Instrument.underlying_instrument_id == equity_id,
                Instrument.expiry == expiry, Instrument.option_type == option_type,
                Instrument.strike.is_not(None), Instrument.strike >= lo, Instrument.strike <= hi,
            )
        )
    ).scalars().all()
    best: tuple[float, float] | None = None
    for option in options:
        oi = await _latest_oi(ctx, option.id)
        if oi is not None and (best is None or oi > best[1]):
            best = (option.strike, oi)
    return best


# ---------------------------------------------------------------------
# Phase A: the 9:20 / 9:25 scan (Steps 1-6)
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
        oi_result = await _total_oi_pct_change(ctx, equity.id, near_future.id, near_future.expiry, today) if near_future else None
        oi_pct_change = oi_result["pct_change"] if oi_result else None

        reasons = []
        if not passes_oi_change(oi_pct_change):
            reasons.append("OI change below threshold")
        if not passes_retracement(opening["high"], opening["low"], opening["close"], direction):
            reasons.append("retraced >= 50%")
        if not nifty_filter_allows(direction, nifty_candle["open"], nifty_candle["close"]):
            reasons.append("Nifty red -- gainers excluded")

        if reasons:
            continue

        max_oi_ce = await _max_oi_strike_near_spot(ctx, equity.id, near_future.expiry, price, "CE") if near_future else None
        max_oi_pe = await _max_oi_strike_near_spot(ctx, equity.id, near_future.expiry, price, "PE") if near_future else None
        # Preview only -- the strike Step 8 actually buys is re-picked at
        # real breakout time in _try_enter, against the price at that
        # moment, which can differ from this scan-time preview if the
        # underlying keeps moving between now and the real breakout.
        setup_option = await _pick_otm_option(ctx, equity.id, near_future.expiry, direction, price) if near_future else None

        setups[symbol] = {
            "equity_instrument_id": str(equity.id),
            "direction": direction,
            "status": "watching",
            "prev_close": prev_close,
            "price_at_scan": price,
            "pct_change": pct_change,
            "oi_pct_change": oi_pct_change,
            "prev_oi": oi_result["prev_total"] if oi_result else None,
            "latest_oi": oi_result["latest_total"] if oi_result else None,
            "oi_detail": oi_result,
            "setup_option_symbol": setup_option.symbol if setup_option else None,
            "setup_option_strike": setup_option.strike if setup_option else None,
            "max_oi_ce_strike": max_oi_ce[0] if max_oi_ce else None,
            "max_oi_ce_oi": max_oi_ce[1] if max_oi_ce else None,
            "max_oi_pe_strike": max_oi_pe[0] if max_oi_pe else None,
            "max_oi_pe_oi": max_oi_pe[1] if max_oi_pe else None,
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
        shortlisted_rows.append({
            "symbol": symbol, "direction": direction, "prev_close": prev_close, "price": price,
            "pct_change": pct_change, "oi_pct_change": oi_pct_change, "oi_detail": oi_result,
            "setup_option_symbol": setup_option.symbol if setup_option else None,
            "setup_option_strike": setup_option.strike if setup_option else None,
            "max_oi_ce": max_oi_ce, "max_oi_pe": max_oi_pe,
        })

    return setups, shortlisted_rows


def _timestamp_line(now_ist: datetime) -> str:
    return f"As of {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST"


def _format_shortlist_row(r: dict) -> str:
    # Cash (equity/spot) price movement spelled out explicitly -- prev
    # close, current price, and the resulting % move -- so the >2%
    # momentum condition (Step 2) can be verified by eye against the raw
    # numbers, not just trusted from the computed percentage alone.
    line = (
        f"{r['symbol']} {r['direction']}: cash {r['prev_close']:.2f} -> {r['price']:.2f} "
        f"({r['pct_change']:+.2f}%, threshold >2%)"
    )

    # Total OI = futures + every CE strike + every PE strike, all for the
    # current running-month expiry (cross-checked against NSE's own OI
    # Spurts methodology, per instruction) -- this combined number is
    # what Step 3's >7% gate actually checks now, not futures OI alone;
    # the per-leg breakdown is shown so each component is independently
    # verifiable, not just the combined total.
    detail = r.get("oi_detail")
    if detail:
        line += (
            f"\n  OI (Total = Futures+CE+PE, current month): yesterday {detail['prev_total']:,.0f} -> "
            f"today {detail['latest_total']:,.0f} ({r['oi_pct_change']:+.1f}%, threshold >7%)"
            f"\n  Futures: {detail['fut_prev']:,.0f} -> {detail['fut_latest']:,.0f} | "
            f"CE (all strikes): {detail['ce_prev']:,.0f} -> {detail['ce_latest']:,.0f} | "
            f"PE (all strikes): {detail['pe_prev']:,.0f} -> {detail['pe_latest']:,.0f}"
        )

    if r.get("setup_option_symbol"):
        line += f"\n  Trade setup strike: {r['setup_option_symbol']} ({r['setup_option_strike']:.0f}, ~2% OTM as of scan time)"

    max_oi_ce, max_oi_pe = r.get("max_oi_ce"), r.get("max_oi_pe")
    if max_oi_ce or max_oi_pe:
        ce_part = f"CE {max_oi_ce[0]:.0f}({max_oi_ce[1]:.0f})" if max_oi_ce else "CE --"
        pe_part = f"PE {max_oi_pe[0]:.0f}({max_oi_pe[1]:.0f})" if max_oi_pe else "PE --"
        line += f"\n  Highest OI strike (same stock, near spot): {ce_part} {pe_part}"
    return line


async def _send_shortlist_alert(ctx, now_ist: datetime, row: dict, *, title_suffix: str = "shortlisted") -> None:
    """One alert/Telegram message per stock -- per instruction, a stock
    shortlisted alongside others no longer gets bundled into one combined
    message; each gets its own, titled with that stock's own symbol."""
    message = f"{_timestamp_line(now_ist)}\n\n{_format_shortlist_row(row)}"
    alert_title = f"F&O Opening Momentum: {row['symbol']} {title_suffix}"
    await create_alert(
        ctx.db, user_id=ctx.portfolio.user_id, alert_type=AlertType.STRATEGY_SIGNAL.value, severity=AlertSeverity.INFO,
        title=alert_title, message=message, object_type="paper_native_deployment", object_id=str(ctx.deployment.id),
    )
    await send_telegram(alert_title, message)


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
    alert_message = f"{_timestamp_line(now_ist)}\n\n{exit_reason}: P&L {pnl:+.2f}"
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
        ctx.state.update(
            session_date=today.isoformat(), shortlist_done=False, second_scan_done=False,
            breakout_marked=False, report_sent=False, setups={},
        )

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
        if shortlisted_rows:
            for row in shortlisted_rows:
                await _send_shortlist_alert(ctx, now_ist, row, title_suffix="shortlisted at 9:20")
        else:
            # Still one confirmation alert when nothing qualifies, so a
            # quiet 9:20 scan reads as "ran, found nothing" rather than
            # being indistinguishable from the scan never having run.
            message = f"{_timestamp_line(now_ist)}\n\n(none)"
            await create_alert(
                ctx.db, user_id=ctx.portfolio.user_id, alert_type=AlertType.STRATEGY_SIGNAL.value, severity=AlertSeverity.INFO,
                title="F&O Opening Momentum: 0 shortlisted at 9:20", message=message,
                object_type="paper_native_deployment", object_id=str(ctx.deployment.id),
            )
            await send_telegram("F&O Opening Momentum: 0 shortlisted at 9:20", message)
        ctx.note("entered" if shortlisted_rows else "skipped", reason=f"9:20 scan: {len(shortlisted_rows)} shortlisted")
        return

    # Second pass, per instruction: re-run the exact same scan at 9:25 to
    # catch any stock that crosses the momentum/OI thresholds a few
    # minutes later than the 9:20 pass. Only ever ADDS newly-qualifying
    # symbols to the shortlist -- anything already shortlisted at 9:20
    # stays, even if its numbers would no longer pass by 9:25 (see module
    # docstring). Deliberately doesn't `return`, so a freshly-added
    # symbol's breakout level still gets marked in this same tick, below.
    if ctx.state["shortlist_done"] and not ctx.state["second_scan_done"] and now_ist.time() >= BREAKOUT_LEVEL_TIME:
        second_setups, second_rows = await _run_scan(ctx, today)
        newly_added = {sym: s for sym, s in second_setups.items() if sym not in ctx.state["setups"]}
        ctx.state["setups"].update(newly_added)
        ctx.state["second_scan_done"] = True
        if newly_added:
            added_rows = [r for r in second_rows if r["symbol"] in newly_added]
            for row in added_rows:
                await _send_shortlist_alert(ctx, now_ist, row, title_suffix="shortlisted at 9:25")
        ctx.note("entered" if newly_added else "hold", reason=f"9:25 second scan: {len(newly_added)} additional shortlisted")

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
        report_message = f"{_timestamp_line(now_ist)}\n\n{summary}\n\n{rows}"
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
