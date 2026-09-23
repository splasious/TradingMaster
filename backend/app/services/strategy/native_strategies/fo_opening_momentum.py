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
one message listing several stocks together. Alongside those, the 9:20
scan always sends one scan-summary message (_send_scan_summary): how many
stocks were scanned, where the live data came from, and every stock that
cleared Step 2's >2% move but was then rejected, with the actual failing
gate(s) and numbers -- so a stock that "should have been caught" shows
up there with the reason it wasn't, instead of vanishing silently. The
9:25 pass sends a summary only when it has something new to say (newly
shortlisted or newly rejected symbols).

Live data (why this module talks to Kite directly): nothing else in the
platform keeps this scanner's inputs current. active_timeframe_sync_scheduler
only refreshes candles for regular PaperDeployment (instrument, timeframe)
pairs, the OI snapshot job only writes 15m bars for options the Kite
WebSocket happens to stream, and TickEngine serves a stale DB close (then
a simulated random walk off it) for any equity it isn't already tracking
-- which, right after a process restart, is every stock at 9:20. So,
whenever a Zerodha account is connected and this isn't a backtest replay:
  - Step 2 reads last price and previous close from one batched Kite
    /quote call for the whole universe (previous close = the quote's own
    ohlc.close, not "the last stored 5m bar before today").
  - Step 3 reads each leg's current OI from Kite /quote too, and fetches
    the future's own previous-day daily candle from Kite if it isn't
    stored yet. A leg only counts toward Total OI when both yesterday's
    and today's reading are known, so a leg with a reading on one side
    only can't skew the % change; the alert shows how many strikes had
    both.
  - Today's 5m candles for Nifty (Step 6), each shortlisted stock's
    opening candle and breakout range (Steps 5/7) and the SMA exit's
    closes (Step 10) are fetched from Kite's historical API and stored in
    ohlcv_candles, only when the latest completed candle isn't already
    stored.
With no connected account (or in a backtest), every read falls back to
the stored-candle/TickEngine path this module always used.

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

import asyncio
import logging
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.alert import AlertSeverity, AlertType
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.paper_trading import PaperNativeTrade
from app.services.alerts.service import create_alert
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.market_data.tick_engine import tick_engine
from app.services.notifications.telegram import send_telegram
from app.services.broker.zerodha_broker import IST, KiteAPIError, ZerodhaKiteBroker

logger = logging.getLogger(__name__)

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
CANDLE_STEP = timedelta(minutes=5)
SESSION_CANDLES = 75  # 09:15-15:30 in 5m candles
NIFTY_SYMBOL = "NIFTY 50"

# Kite rate limits: /quote ~1 req/s (and at most 500 instruments per
# call), historical ~3 req/s -- paced below so a busy scan doesn't burn
# _request()'s bounded 429 retries.
KITE_QUOTE_BATCH = 250
KITE_QUOTE_PACING_SECONDS = 1.0
KITE_HISTORICAL_PACING_SECONDS = 0.35


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


# ---------------------------------------------------------------------
# Live Kite reads (see module docstring's "Live data" section)
# ---------------------------------------------------------------------
async def _live_broker(ctx) -> ZerodhaKiteBroker | None:
    """The one connected Zerodha session, or None in a backtest replay or
    when no account is connected -- every caller then falls back to the
    stored-candle/TickEngine reads."""
    if getattr(ctx, "is_backtest", False):
        return None
    creds = await find_connected_zerodha_credentials(ctx.db)
    if creds is None:
        return None
    broker = ZerodhaKiteBroker()
    broker._api_key = creds["api_key"]
    broker._access_token = creds["access_token"]
    return broker


async def _fetch_quotes(broker: ZerodhaKiteBroker, keys: list[str]) -> tuple[dict[str, dict], str | None]:
    """Batched Kite /quote reads -- ({"EXCHANGE:SYMBOL": quote}, error or
    None). A failed batch is skipped (its keys just come back missing), with
    the last error returned so the scan summary can say so."""
    quotes: dict[str, dict] = {}
    error: str | None = None
    batches = [keys[i : i + KITE_QUOTE_BATCH] for i in range(0, len(keys), KITE_QUOTE_BATCH)]
    for i, batch in enumerate(batches):
        if i:
            await asyncio.sleep(KITE_QUOTE_PACING_SECONDS)
        try:
            quotes.update(await broker.get_quote_batch(batch))
        except KiteAPIError as exc:
            error = str(exc)
            logger.warning("F&O Opening Momentum: Kite quote batch failed (%d instruments): %s", len(batch), exc)
    return quotes, error


async def _store_candles(ctx, instrument_id: uuid.UUID, timeframe: str, bars: list[dict]) -> None:
    """Inserts Kite bars into ohlcv_candles, skipping any (instrument,
    timeframe, ts) already stored -- ON CONFLICT DO NOTHING rather than
    select-then-insert, since active_timeframe_sync_scheduler may be
    writing the same pair concurrently from its own session."""
    if not bars:
        return
    rows = [
        {
            "id": uuid.uuid4(), "instrument_id": instrument_id, "timeframe": timeframe,
            "ts": as_aware_utc(bar["ts"]).astimezone(timezone.utc),
            "open": bar["open"], "high": bar["high"], "low": bar["low"], "close": bar["close"],
            "volume": bar.get("volume"), "open_interest": bar.get("open_interest"), "source": "zerodha_kite",
        }
        for bar in bars
    ]
    if ctx.db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await ctx.db.execute(
        insert(OhlcvCandle).values(rows).on_conflict_do_nothing(index_elements=["instrument_id", "timeframe", "ts"])
    )


async def _sync_today_5m(ctx, broker: ZerodhaKiteBroker | None, instrument: Instrument, today: date, now_ist: datetime) -> None:
    """Pulls today's completed 5m candles for one instrument from Kite into
    ohlcv_candles -- a no-op when the latest completed candle is already
    stored, so calling it every tick costs one indexed lookup, not a Kite
    call. Only completed candles are stored: a still-forming one would be
    kept forever by the insert's duplicate-skip, frozen mid-candle."""
    if broker is None:
        return
    session_open = datetime.combine(today, MARKET_OPEN, tzinfo=IST)
    closed = min(int((now_ist - session_open) / CANDLE_STEP), SESSION_CANDLES)
    if closed < 1:
        return
    latest_closed_start = session_open + CANDLE_STEP * (closed - 1)
    already_stored = (
        await ctx.db.execute(
            select(OhlcvCandle.id)
            .where(
                OhlcvCandle.instrument_id == instrument.id, OhlcvCandle.timeframe == CANDLE_TIMEFRAME,
                OhlcvCandle.ts == latest_closed_start.astimezone(timezone.utc),
            )
            .limit(1)
        )
    ).first()
    if already_stored:
        return
    try:
        bars = await broker.get_historical_data(instrument.external_ref, CANDLE_TIMEFRAME, session_open, now_ist, instrument.exchange)
    except KiteAPIError as exc:
        logger.warning("F&O Opening Momentum: Kite 5m fetch failed for %s: %s", instrument.symbol, exc)
        return
    finally:
        await asyncio.sleep(KITE_HISTORICAL_PACING_SECONDS)
    completed = [
        bar for bar in bars
        if as_aware_utc(bar["ts"]) >= session_open and as_aware_utc(bar["ts"]) + CANDLE_STEP <= now_ist
    ]
    await _store_candles(ctx, instrument.id, CANDLE_TIMEFRAME, completed)


async def _ensure_prev_day_oi(ctx, broker: ZerodhaKiteBroker | None, future: Instrument, today: date) -> None:
    """Fetches the future's recent daily candles (with OI) from Kite when
    no previous-day "1d" reading is stored yet -- Step 3's baseline for the
    futures leg. One call per shortlisting candidate, only when missing."""
    if broker is None or await _prev_day_oi(ctx, future.id, today) is not None:
        return
    start = datetime.combine(today - timedelta(days=10), dtime(0, 0), tzinfo=IST)
    end = datetime.combine(today, MARKET_OPEN, tzinfo=IST)
    try:
        bars = await broker.get_historical_data(future.external_ref, "1d", start, end, future.exchange)
    except KiteAPIError as exc:
        logger.warning("F&O Opening Momentum: Kite daily OI fetch failed for %s: %s", future.symbol, exc)
        return
    finally:
        await asyncio.sleep(KITE_HISTORICAL_PACING_SECONDS)
    today_start = datetime.combine(today, dtime(0, 0), tzinfo=IST)
    await _store_candles(ctx, future.id, "1d", [bar for bar in bars if as_aware_utc(bar["ts"]) < today_start])


async def _notify(ctx, title: str, message: str, *, alert_type: str = AlertType.STRATEGY_SIGNAL.value, severity: AlertSeverity = AlertSeverity.INFO) -> None:
    """In-app alert + Telegram push -- skipped in a backtest replay, which
    would otherwise push a notification for every replayed day."""
    if getattr(ctx, "is_backtest", False):
        return
    await create_alert(
        ctx.db, user_id=ctx.portfolio.user_id, alert_type=alert_type, severity=severity,
        title=title, message=message, object_type="paper_native_deployment", object_id=str(ctx.deployment.id),
    )
    await send_telegram(title, message)


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
        "close": rows[-1].close, "bars": len(rows),
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


async def _expiry_options(ctx, equity_id: uuid.UUID, expiry: date, option_type: str | None = None) -> list[Instrument]:
    query = select(Instrument).where(
        Instrument.instrument_type == "option", Instrument.underlying_instrument_id == equity_id,
        Instrument.expiry == expiry, Instrument.strike.is_not(None),
    )
    if option_type is not None:
        query = query.where(Instrument.option_type == option_type)
    return list((await ctx.db.execute(query)).scalars().all())


async def _leg_oi(ctx, instrument_id: uuid.UUID, today: date, live_oi: dict | None) -> tuple[float | None, float | None]:
    """(yesterday's EOD OI, today's latest OI) for one contract. With live
    quotes available (live_oi not None) today's reading comes only from
    them -- never a stale stored bar -- so a contract Kite returned no
    quote for reads as unknown rather than as yesterday's number again."""
    prev = await _prev_day_oi(ctx, instrument_id, today)
    latest = live_oi.get(instrument_id) if live_oi is not None else await _latest_oi(ctx, instrument_id)
    return prev, latest


async def _sum_options_oi(
    ctx, equity_id: uuid.UUID, expiry: date, option_type: str, today: date, live_oi: dict | None = None,
) -> tuple[float, float, int, int]:
    """(yesterday_total, today_total, strikes_counted, strikes_listed) OI
    across every strike of one option_type ("CE" or "PE") for the given
    (current running month) expiry. A strike only counts when BOTH
    readings are known -- adding a strike's today-OI with no matching
    yesterday-OI (or vice versa) would move the % change by that strike's
    whole OI, not by its change."""
    options = await _expiry_options(ctx, equity_id, expiry, option_type)
    prev_total = 0.0
    latest_total = 0.0
    counted = 0
    for option in options:
        prev, latest = await _leg_oi(ctx, option.id, today, live_oi)
        if prev is None or latest is None:
            continue
        prev_total += prev
        latest_total += latest
        counted += 1
    return prev_total, latest_total, counted, len(options)


async def _total_oi_pct_change(
    ctx, equity_id: uuid.UUID, future_instrument_id: uuid.UUID, expiry: date, today: date, live_oi: dict | None = None,
) -> dict | None:
    """Total OI = the near-month future's own OI + every CE strike's OI +
    every PE strike's OI, all for that SAME current-running-month expiry --
    per instruction (cross-checked against NSE's own OI Spurts
    methodology), yesterday's EOD reading vs today's latest, exactly the
    same EOD-to-now convention the futures-only reading used before this
    replaced it as Step 3's actual >7% gate. `live_oi` ({instrument_id:
    oi} from Kite /quote) supplies today's side when given. Each leg
    counts only when both of its readings are known (see
    _sum_options_oi). Returns None if the yesterday total is unavailable
    or zero (nothing to compare against); the full per-leg breakdown is
    returned alongside the total so the notification can show its
    components, not just the combined number."""
    fut_prev, fut_latest = await _leg_oi(ctx, future_instrument_id, today, live_oi)
    if fut_prev is None or fut_latest is None:
        fut_prev = fut_latest = None
    ce_prev, ce_latest, ce_counted, ce_listed = await _sum_options_oi(ctx, equity_id, expiry, "CE", today, live_oi)
    pe_prev, pe_latest, pe_counted, pe_listed = await _sum_options_oi(ctx, equity_id, expiry, "PE", today, live_oi)

    prev_total = (fut_prev or 0.0) + ce_prev + pe_prev
    latest_total = (fut_latest or 0.0) + ce_latest + pe_latest
    if not prev_total:
        return None
    return {
        "pct_change": (latest_total - prev_total) / prev_total * 100.0,
        "prev_total": prev_total, "latest_total": latest_total,
        "fut_prev": fut_prev or 0.0, "fut_latest": fut_latest or 0.0, "fut_counted": fut_prev is not None,
        "ce_prev": ce_prev, "ce_latest": ce_latest, "ce_counted": ce_counted, "ce_listed": ce_listed,
        "pe_prev": pe_prev, "pe_latest": pe_latest, "pe_counted": pe_counted, "pe_listed": pe_listed,
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


async def _max_oi_strike_near_spot(
    ctx, equity_id: uuid.UUID, expiry: date, spot: float, option_type: str, live_oi: dict | None = None,
) -> tuple[float, float] | None:
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
        oi = live_oi.get(option.id) if live_oi is not None else await _latest_oi(ctx, option.id)
        if oi is not None and (best is None or oi > best[1]):
            best = (option.strike, oi)
    return best


# ---------------------------------------------------------------------
# Phase A: the 9:20 / 9:25 scan (Steps 1-6)
# ---------------------------------------------------------------------
async def _universe(ctx, today: date) -> list[tuple[Instrument, Instrument]]:
    """Step 1: (equity, its nearest unexpired future), one pair per stock --
    the catalog can hold several months' futures (and lapsed ones) for the
    same underlying, and scanning per contract would evaluate, and alert
    on, the same stock more than once."""
    futures = (
        await ctx.db.execute(
            select(Instrument)
            .where(
                Instrument.instrument_type == "future", Instrument.underlying_instrument_id.is_not(None),
                Instrument.expiry.is_not(None), Instrument.expiry >= today,
            )
            .order_by(Instrument.expiry)
        )
    ).scalars().all()
    nearest: dict[uuid.UUID, Instrument] = {}
    for fut in futures:
        nearest.setdefault(fut.underlying_instrument_id, fut)
    if not nearest:
        return []
    equities = (await ctx.db.execute(select(Instrument).where(Instrument.id.in_(list(nearest))))).scalars().all()
    return sorted(((equity, nearest[equity.id]) for equity in equities), key=lambda pair: pair[0].symbol)


async def _run_scan(ctx, today: date, now_ist: datetime, broker: ZerodhaKiteBroker | None) -> dict:
    """Steps 1-6 over the whole universe. Returns:
      setups       -- shortlisted stocks' sub-state, keyed by symbol
      shortlisted  -- alert rows for those same stocks
      rejected     -- every stock that cleared Step 2's >2% move but
                      failed a later gate, with the gate(s) and numbers
      counts, data_source -- for the scan summary
      error        -- set (with everything else empty) when the scan
                      can't run at all, e.g. no Nifty 9:15-9:20 candle
                      yet: Step 6 can't be judged without it."""
    result = {"setups": {}, "shortlisted": [], "rejected": [], "counts": None, "data_source": None, "error": None}
    nifty = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == NIFTY_SYMBOL, Instrument.exchange == "NSE"))
    ).scalar_one_or_none()
    if nifty is None:
        result["error"] = f"no {NIFTY_SYMBOL} instrument in the catalog"
        return result
    await _sync_today_5m(ctx, broker, nifty, today, now_ist)
    nifty_candle = await _candle_range(
        ctx, nifty.id, datetime.combine(today, MARKET_OPEN, tzinfo=IST), datetime.combine(today, OPENING_CANDLE_END, tzinfo=IST),
    )
    if nifty_candle is None:
        result["error"] = f"no {NIFTY_SYMBOL} 9:15-9:20 candle available" + ("" if broker else " (no connected Zerodha account to fetch it)")
        return result

    universe = await _universe(ctx, today)
    quotes: dict[str, dict] = {}
    data_source = "stored candles/TickEngine (no connected Zerodha account)"
    if broker is not None and universe:
        quotes, quote_error = await _fetch_quotes(broker, [f"{equity.exchange}:{equity.external_ref}" for equity, _ in universe])
        if not quotes:
            data_source = f"stored candles/TickEngine (Kite quotes failed: {quote_error or 'none returned'})"
        else:
            data_source = "Kite live quotes" + (f" (some batches failed: {quote_error})" if quote_error else "")
    # Every later Kite read in this scan is skipped too once quotes have
    # failed outright -- same broken session, no point retrying per stock.
    kite = broker if quotes else None

    counts = {"scanned": len(universe), "no_price": 0, "below_momentum": 0}
    candidates: list[tuple[Instrument, Instrument, float, float, float]] = []
    for equity, future in universe:
        if kite is not None:
            quote = quotes.get(f"{equity.exchange}:{equity.external_ref}") or {}
            price = quote.get("last_price")
            prev_close = (quote.get("ohlc") or {}).get("close")
        else:
            price = await ctx.get_price(equity.id)
            prev_close = await _prev_close(ctx, equity.id, today)
        if not price or not prev_close:
            counts["no_price"] += 1
            continue
        price, prev_close = float(price), float(prev_close)
        pct_change = (price - prev_close) / prev_close * 100.0
        if not passes_momentum(pct_change):
            counts["below_momentum"] += 1
            continue
        candidates.append((equity, future, price, prev_close, pct_change))

    # Today's OI for every leg of every candidate, in as few /quote calls
    # as possible, rather than whatever OI happens to be stored.
    live_oi: dict[uuid.UUID, float] | None = None
    if kite is not None and candidates:
        contract_ids: dict[str, uuid.UUID] = {}
        for equity, future, *_ in candidates:
            contract_ids[f"{future.exchange}:{future.external_ref}"] = future.id
            for option in await _expiry_options(ctx, equity.id, future.expiry):
                contract_ids[f"{option.exchange}:{option.external_ref}"] = option.id
        oi_quotes, _ = await _fetch_quotes(kite, list(contract_ids))
        live_oi = {
            contract_ids[key]: float(quote["oi"]) for key, quote in oi_quotes.items()
            if key in contract_ids and quote.get("oi") is not None
        } or None

    for equity, future, price, prev_close, pct_change in candidates:
        symbol = equity.symbol
        direction = momentum_direction(pct_change)

        await _ensure_prev_day_oi(ctx, kite, future, today)
        oi_result = await _total_oi_pct_change(ctx, equity.id, future.id, future.expiry, today, live_oi)
        oi_pct_change = oi_result["pct_change"] if oi_result else None

        await _sync_today_5m(ctx, kite, equity, today, now_ist)
        opening = await _candle_range(
            ctx, equity.id, datetime.combine(today, MARKET_OPEN, tzinfo=IST), datetime.combine(today, OPENING_CANDLE_END, tzinfo=IST),
        )

        reasons = []
        if oi_result is None:
            reasons.append("no Total OI baseline (yesterday's OI not on file)")
        elif not passes_oi_change(oi_pct_change):
            reasons.append(f"Total OI {oi_pct_change:+.1f}% (needs beyond +/-{OI_CHANGE_PCT:.0f}%)")
        if opening is None:
            reasons.append("no 9:15-9:20 candle")
        elif not passes_retracement(opening["high"], opening["low"], opening["close"], direction):
            retraced = retracement_pct(opening["high"], opening["low"], opening["close"], direction)
            reasons.append(f"9:15-9:20 candle retraced {retraced:.0f}% (needs <{MAX_RETRACEMENT_PCT:.0f}%)")
        if not nifty_filter_allows(direction, nifty_candle["open"], nifty_candle["close"]):
            reasons.append("Nifty 9:15-9:20 candle red -- gainers excluded")

        if reasons:
            result["rejected"].append({
                "symbol": symbol, "direction": direction, "prev_close": prev_close, "price": price,
                "pct_change": pct_change, "oi_pct_change": oi_pct_change, "reasons": reasons,
            })
            continue

        if kite is not None:
            # Seeds the price _try_enter's breakout check reads -- otherwise
            # its first ctx.get_price() on a freshly restarted process is a
            # stale stored close, until kite_rest_price_feed's next poll.
            tick_engine.set_real_price(equity.id, price, "kite_rest")
        max_oi_ce = await _max_oi_strike_near_spot(ctx, equity.id, future.expiry, price, "CE", live_oi)
        max_oi_pe = await _max_oi_strike_near_spot(ctx, equity.id, future.expiry, price, "PE", live_oi)
        # Preview only -- the strike Step 8 actually buys is re-picked at
        # real breakout time in _try_enter, against the price at that
        # moment, which can differ from this scan-time preview if the
        # underlying keeps moving between now and the real breakout.
        setup_option = await _pick_otm_option(ctx, equity.id, future.expiry, direction, price)

        result["setups"][symbol] = {
            "equity_instrument_id": str(equity.id),
            "direction": direction,
            "status": "watching",
            "prev_close": prev_close,
            "price_at_scan": price,
            "pct_change": pct_change,
            "oi_pct_change": oi_pct_change,
            "prev_oi": oi_result["prev_total"],
            "latest_oi": oi_result["latest_total"],
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
        result["shortlisted"].append({
            "symbol": symbol, "direction": direction, "prev_close": prev_close, "price": price,
            "pct_change": pct_change, "oi_pct_change": oi_pct_change, "oi_detail": oi_result,
            "setup_option_symbol": setup_option.symbol if setup_option else None,
            "setup_option_strike": setup_option.strike if setup_option else None,
            "max_oi_ce": max_oi_ce, "max_oi_pe": max_oi_pe,
        })

    # Nearest misses first (fewest failed gates, then biggest move) -- the
    # in-app copy of the summary is cut to the alert column's length.
    result["rejected"].sort(key=lambda r: (len(r["reasons"]), -abs(r["pct_change"])))
    result["counts"] = counts
    result["data_source"] = data_source
    return result


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
        fut_note = "" if detail.get("fut_counted", True) else " (not counted: a reading is missing)"
        ce_strikes = f"{detail['ce_counted']}/{detail['ce_listed']} strikes" if "ce_counted" in detail else "all strikes"
        pe_strikes = f"{detail['pe_counted']}/{detail['pe_listed']} strikes" if "pe_counted" in detail else "all strikes"
        line += (
            f"\n  OI (Total = Futures+CE+PE, current month): yesterday {detail['prev_total']:,.0f} -> "
            f"today {detail['latest_total']:,.0f} ({r['oi_pct_change']:+.1f}%, threshold >7%)"
            f"\n  Futures: {detail['fut_prev']:,.0f} -> {detail['fut_latest']:,.0f}{fut_note} | "
            f"CE ({ce_strikes}): {detail['ce_prev']:,.0f} -> {detail['ce_latest']:,.0f} | "
            f"PE ({pe_strikes}): {detail['pe_prev']:,.0f} -> {detail['pe_latest']:,.0f}"
        )

    if r.get("setup_option_symbol"):
        line += f"\n  Trade setup strike: {r['setup_option_symbol']} ({r['setup_option_strike']:.0f}, ~2% OTM as of scan time)"

    max_oi_ce, max_oi_pe = r.get("max_oi_ce"), r.get("max_oi_pe")
    if max_oi_ce or max_oi_pe:
        ce_part = f"CE {max_oi_ce[0]:.0f}({max_oi_ce[1]:.0f})" if max_oi_ce else "CE --"
        pe_part = f"PE {max_oi_pe[0]:.0f}({max_oi_pe[1]:.0f})" if max_oi_pe else "PE --"
        line += f"\n  Highest OI strike (same stock, near spot): {ce_part} {pe_part}"
    return line


def _format_rejected_row(r: dict) -> str:
    oi_part = f"Total OI {r['oi_pct_change']:+.1f}%" if r["oi_pct_change"] is not None else "Total OI n/a"
    return (
        f"{r['symbol']} {r['direction']}: cash {r['prev_close']:.2f} -> {r['price']:.2f} ({r['pct_change']:+.2f}%), "
        f"{oi_part} -- rejected: {'; '.join(r['reasons'])}"
    )


async def _send_shortlist_alert(ctx, now_ist: datetime, row: dict, *, title_suffix: str = "shortlisted") -> None:
    """One alert/Telegram message per stock -- per instruction, a stock
    shortlisted alongside others no longer gets bundled into one combined
    message; each gets its own, titled with that stock's own symbol."""
    message = f"{_timestamp_line(now_ist)}\n\n{_format_shortlist_row(row)}"
    await _notify(ctx, f"F&O Opening Momentum: {row['symbol']} {title_suffix}", message)


async def _send_scan_summary(ctx, now_ist: datetime, label: str, scan: dict, shortlisted: list[str], rejected: list[dict]) -> None:
    """The per-scan diagnostic message (see module docstring): what was
    scanned, from which data, and why each stock that cleared the >2%
    move didn't make the shortlist."""
    lines = [_timestamp_line(now_ist), ""]
    if scan["error"]:
        lines.append(f"Scan could not run: {scan['error']}")
    else:
        counts = scan["counts"]
        lines.append(f"Scanned {counts['scanned']} F&O stocks -- data: {scan['data_source']}")
        lines.append(f"Below the >2% move: {counts['below_momentum']} | No price/previous close: {counts['no_price']}")
    lines.append("")
    lines.append(f"Shortlisted ({len(shortlisted)}): {', '.join(shortlisted) if shortlisted else 'none'}")
    if rejected:
        lines.append("")
        lines.append(f"Cleared the >2% move but rejected ({len(rejected)}):")
        lines.extend(_format_rejected_row(r) for r in rejected)
    title = f"F&O Opening Momentum: {len(shortlisted)} {'more ' if label != '9:20' else ''}shortlisted at {label}"
    await _notify(ctx, title, "\n".join(lines))


def _scan_log_entry(now_ist: datetime, scan: dict) -> dict:
    """JSON-safe record of one scan pass, kept on ctx.state["scan_log"] so
    the deployment's own state shows why each candidate was rejected."""
    return {
        "at": now_ist.isoformat(), "error": scan["error"], "data_source": scan["data_source"], "counts": scan["counts"],
        "shortlisted": [r["symbol"] for r in scan["shortlisted"]], "rejected": scan["rejected"],
    }


async def _mark_breakout_levels(ctx, setups: dict, today: date, now_ist: datetime, broker: ZerodhaKiteBroker | None) -> None:
    """Step 7 for every watching setup still missing its levels -- retried
    each tick until both the 9:15 and 9:20 candles are stored, since a
    range off the 9:15 candle alone would be narrower than the real
    9:15-9:25 one."""
    for setup in setups.values():
        if setup["status"] != "watching" or setup["breakout_high"] is not None:
            continue
        equity_id = uuid.UUID(setup["equity_instrument_id"])
        equity = await ctx.db.get(Instrument, equity_id)
        if equity is not None:
            await _sync_today_5m(ctx, broker, equity, today, now_ist)
        rng = await _candle_range(ctx, equity_id, datetime.combine(today, MARKET_OPEN, tzinfo=IST), datetime.combine(today, BREAKOUT_LEVEL_TIME, tzinfo=IST))
        if rng is None or rng["bars"] < 2:
            continue
        setup["breakout_high"] = rng["high"]
        setup["breakout_low"] = rng["low"]


# ---------------------------------------------------------------------
# Phase B/C: entry, SMA exit, EOD close (Steps 7-11)
# ---------------------------------------------------------------------
async def _try_enter(ctx, symbol: str, setup: dict, now_ist: datetime, broker: ZerodhaKiteBroker | None = None) -> None:
    if setup["status"] != "watching":
        return
    if now_ist.time() >= BREAKOUT_CUTOFF:
        setup["status"] = "no_trigger"
        return
    if setup["breakout_high"] is None:
        return

    equity_id = uuid.UUID(setup["equity_instrument_id"])
    price = await ctx.get_price(equity_id)
    if price is None:
        return
    direction = setup["direction"]
    breakout_hit = price > setup["breakout_high"] if direction == "CE" else price < setup["breakout_low"]
    if not breakout_hit:
        return

    near_future = await _nearest_future(ctx, equity_id, now_ist.date())
    expiry = near_future.expiry if near_future else None
    if expiry is None or in_expiry_blackout(now_ist.date(), expiry):
        setup["status"] = "blackout"
        return

    option = await _pick_otm_option(ctx, equity_id, expiry, direction, price)
    if option is None:
        return
    if broker is not None:
        # The option was never read before this instant, so TickEngine has
        # no live price for it yet -- fill at Kite's actual LTP, not the
        # last stored candle close.
        try:
            ltp = await broker.get_ltp(option.exchange, option.external_ref)
            tick_engine.set_real_price(option.id, ltp["price"], "kite_rest")
        except KiteAPIError as exc:
            logger.warning("F&O Opening Momentum: Kite LTP failed for %s: %s", option.symbol, exc)
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
    await _notify(
        ctx, f"{symbol} {setup['direction']} closed", f"{_timestamp_line(now_ist)}\n\n{exit_reason}: P&L {pnl:+.2f}",
        alert_type=AlertType.ORDER_EXECUTED.value, severity=AlertSeverity.INFO if pnl >= 0 else AlertSeverity.WARNING,
    )
    ctx.note("exited", signal="SELL", reason=f"{symbol}: {exit_reason}, P&L {pnl:+.2f}")


async def _manage_position(ctx, symbol: str, setup: dict, now_ist: datetime, today: date, broker: ZerodhaKiteBroker | None = None) -> None:
    if setup["status"] != "triggered":
        return

    equity_id = uuid.UUID(setup["equity_instrument_id"])
    equity = await ctx.db.get(Instrument, equity_id)
    if equity is not None:
        await _sync_today_5m(ctx, broker, equity, today, now_ist)
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
            breakout_marked=False, report_sent=False, setups={}, scan_log={},
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

    broker = await _live_broker(ctx) if now_ist.time() >= OPENING_CANDLE_END and not ctx.state["report_sent"] else None

    if not ctx.state["shortlist_done"] and now_ist.time() >= OPENING_CANDLE_END:
        scan = await _run_scan(ctx, today, now_ist, broker)
        if scan["error"] and now_ist.time() < BREAKOUT_LEVEL_TIME:
            # Most often Nifty's 9:15 candle just isn't published yet in the
            # first seconds after 9:20 -- retry next tick rather than marking
            # the day's scan done with nothing; past 9:25, give up and say so.
            ctx.note("hold", reason=f"9:20 scan waiting: {scan['error']}")
            return
        ctx.state["setups"] = scan["setups"]
        ctx.state["shortlist_done"] = True
        ctx.state.setdefault("scan_log", {})["9:20"] = _scan_log_entry(now_ist, scan)
        for row in scan["shortlisted"]:
            await _send_shortlist_alert(ctx, now_ist, row, title_suffix="shortlisted at 9:20")
        # Always sent, including when nothing qualifies, so a quiet 9:20
        # scan reads as "ran, found nothing -- and here's why" rather than
        # being indistinguishable from the scan never having run.
        await _send_scan_summary(ctx, now_ist, "9:20", scan, [r["symbol"] for r in scan["shortlisted"]], scan["rejected"])
        ctx.note(
            "entered" if scan["shortlisted"] else "skipped",
            reason=f"9:20 scan: {len(scan['shortlisted'])} shortlisted, {len(scan['rejected'])} rejected after the >2% move"
                   + (f" -- {scan['error']}" if scan["error"] else ""),
        )
        return

    # Second pass, per instruction: re-run the exact same scan at 9:25 to
    # catch any stock that crosses the momentum/OI thresholds a few
    # minutes later than the 9:20 pass. Only ever ADDS newly-qualifying
    # symbols to the shortlist -- anything already shortlisted at 9:20
    # stays, even if its numbers would no longer pass by 9:25 (see module
    # docstring). Deliberately doesn't `return`, so a freshly-added
    # symbol's breakout level still gets marked in this same tick, below.
    if ctx.state["shortlist_done"] and not ctx.state["second_scan_done"] and now_ist.time() >= BREAKOUT_LEVEL_TIME:
        scan = await _run_scan(ctx, today, now_ist, broker)
        newly_added = {sym: s for sym, s in scan["setups"].items() if sym not in ctx.state["setups"]}
        already_reported = set(ctx.state["setups"]) | {r["symbol"] for r in (ctx.state.get("scan_log", {}).get("9:20") or {}).get("rejected", [])}
        new_rejects = [r for r in scan["rejected"] if r["symbol"] not in already_reported]
        ctx.state["setups"].update(newly_added)
        ctx.state["second_scan_done"] = True
        ctx.state.setdefault("scan_log", {})["9:25"] = _scan_log_entry(now_ist, scan)
        for row in scan["shortlisted"]:
            if row["symbol"] in newly_added:
                await _send_shortlist_alert(ctx, now_ist, row, title_suffix="shortlisted at 9:25")
        if newly_added or new_rejects:
            await _send_scan_summary(ctx, now_ist, "9:25", scan, list(newly_added), new_rejects)
        ctx.note("entered" if newly_added else "hold", reason=f"9:25 second scan: {len(newly_added)} additional shortlisted")

    # Retried each tick for any setup whose 9:15-9:25 range isn't complete
    # yet (see _mark_breakout_levels) -- a no-op once every setup has one.
    if ctx.state["shortlist_done"] and now_ist.time() >= BREAKOUT_LEVEL_TIME:
        if now_ist.time() < BREAKOUT_CUTOFF:
            await _mark_breakout_levels(ctx, ctx.state["setups"], today, now_ist, broker)
        ctx.state["breakout_marked"] = True

    if ctx.state["breakout_marked"]:
        for symbol, setup in ctx.state["setups"].items():
            await _try_enter(ctx, symbol, setup, now_ist, broker)
            await _manage_position(ctx, symbol, setup, now_ist, today, broker)

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
        await _notify(ctx, "F&O Opening Momentum: 3:10pm report", report_message)
        ctx.state["report_sent"] = True
        ctx.note("exited", reason=summary)
        return

    ctx.note("hold", reason=f"{len(ctx.state.get('setups', {}))} setups tracked")
