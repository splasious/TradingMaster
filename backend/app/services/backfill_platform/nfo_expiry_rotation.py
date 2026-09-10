"""Keeps a rolling window of NIFTY/BANKNIFTY NFO option expiries backfilled
automatically, so the Options Dashboard's PCR/chain data doesn't go stale
as the nearest weekly expiry lapses and rolls over to the next one -- the
original ±20-strikes-around-ATM, 2-expiry backfill earlier this session
was a manual, one-off pass (search a symbol -> add to a watchlist ->
backfill) with nothing keeping it current.

Every underlying this maintains is derived from what's already tracked
(distinct `Instrument.underlying_instrument_id` among `exchange == "NFO"`
rows) rather than a hardcoded NIFTY/BANKNIFTY list -- today that's exactly
those two, per the earlier "keep only Nifty and bank nifty" instruction,
without hardcoding the names here.

No pruning of expired contracts: this is purely additive, ensuring a
target set of expiries exist and no-op once they do. Historical OI/PCR
data for a lapsed contract stays useful for backtesting, same as this
app keeps full equity history rather than deleting old candles.

Expiry dates and strike lists both come straight from Kite's own live NFO
instrument dump (`ZerodhaKiteBroker.get_instruments("NFO")`, cached 30 min
process-wide) rather than any hardcoded NSE-holiday-aware "next Tuesday"
date math or an assumed 50/100-point strike step -- Kite's dump already
*is* the real, always-current expiry/strike calendar, so there's nothing
to compute that isn't already sitting in that response.

Follows `kite_ticker_service.py`'s now-established pattern for a
background asyncio task safely resolving and using the one connected
Zerodha account's session -- `live_sync_scheduler.py`'s older reasoning
("no single 'the' background session to run this under") predates that
and doesn't apply here.
"""

import asyncio
import logging
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.backfill_platform import BfBackfillJob
from app.models.instrument import Instrument
from app.services.backfill_platform.catalog_sync import UNDERLYING_NAME_ALIASES
from app.services.backfill_platform.jobs import run_bf_backfill_job
from app.services.backfill_platform.symbols import get_or_create_symbol
from app.services.broker.kite_ticker_service import find_connected_zerodha_account, find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import IST, KiteAPIError, ZerodhaKiteBroker

logger = logging.getLogger(__name__)

# How many upcoming expiries to keep backfilled per underlying -- covers
# the near-term weeklies (and whichever of them is also the month's
# monthly expiry) a realistic market-wide PCR would be built from.
EXPIRIES_TO_MAINTAIN = 4
# Same ±20-around-ATM convention as the original manual backfill.
STRIKE_WINDOW = 20
# Matches the PCR endpoint's own default timeframe -- one bounded pass per
# new expiry rather than several timeframes' worth of Kite API calls for
# granularities nobody's asked for yet.
BACKFILL_TIMEFRAME = "15m"
# Expiries only roll over weekly/monthly -- cheap to check far more often
# than that (one cached instrument-dump read when nothing's missing), but
# no need to.
CHECK_INTERVAL_SECONDS = 6 * 3600

_KITE_NAME_BY_UNDERLYING_SYMBOL = {v: k for k, v in UNDERLYING_NAME_ALIASES.items()}


def _kite_name_for(underlying_symbol: str) -> str:
    """Reverse of catalog_sync.py's own alias table (Kite "name" -> our
    Instrument.symbol) -- falls back to the symbol itself for an
    underlying that was never aliased (an exact-match case already)."""
    return _KITE_NAME_BY_UNDERLYING_SYMBOL.get(underlying_symbol, underlying_symbol)


def _select_strike_window(available_strikes: list[float], spot: float, window: int) -> list[float]:
    """The `window` strikes either side of whichever available strike is
    closest to `spot` -- read off Kite's own actual strike list for this
    expiry rather than assumed from a hardcoded 50/100-point step, so this
    is correct for NIFTY and BANKNIFTY (or anything else) without special-
    casing either."""
    if not available_strikes:
        return []
    strikes = sorted(set(available_strikes))
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    return strikes[max(0, atm_idx - window) : atm_idx + window + 1]


def _target_expiries(nfo_rows: list[dict], kite_name: str, today: date, count: int) -> list[date]:
    """Distinct, still-upcoming expiries for one underlying, straight out
    of Kite's live NFO dump -- sorted ascending, capped at `count`."""
    expiries: set[date] = set()
    for row in nfo_rows:
        if row.get("name") != kite_name:
            continue
        if (row.get("instrument_type") or "").upper() not in ("CE", "PE"):
            continue
        expiry_str = row.get("expiry")
        if not expiry_str:
            continue
        expiry = expiry_str if isinstance(expiry_str, date) else date.fromisoformat(expiry_str)
        if expiry >= today:
            expiries.add(expiry)
    return sorted(expiries)[:count]


async def _tracked_underlyings(db: AsyncSession) -> list[Instrument]:
    """Every Instrument actually referenced as an NFO underlying -- same
    derivation the /options/underlyings endpoint uses, not a hardcoded
    index list (options/endpoints.py's own list_underlyings)."""
    referenced = (
        select(Instrument.underlying_instrument_id)
        .where(Instrument.underlying_instrument_id.is_not(None), Instrument.exchange == "NFO")
        .distinct()
    )
    result = await db.execute(select(Instrument).where(Instrument.id.in_(referenced)))
    return list(result.scalars().all())


async def _ensure_underlying_expiries(
    db: AsyncSession, broker: ZerodhaKiteBroker, underlying: Instrument, account_user_id: uuid.UUID
) -> int:
    """Backfills every strike in the ATM±STRIKE_WINDOW window, for every
    expiry among this underlying's nearest EXPIRIES_TO_MAINTAIN not
    already present in the Instrument catalog. Returns how many new
    contracts were queued (0 when everything's already covered)."""
    nfo_rows = await broker.get_instruments("NFO")
    kite_name = _kite_name_for(underlying.symbol)
    # IST calendar date, not the server's raw UTC one -- NSE expiries are
    # an IST concept, and the server's UTC "today" runs ~5.5h behind IST
    # every day between UTC 18:30 and 24:00 (00:00-05:30 IST).
    today_ist = datetime.now(timezone.utc).astimezone(IST).date()
    targets = _target_expiries(nfo_rows, kite_name, today_ist, EXPIRIES_TO_MAINTAIN)
    if not targets:
        return 0

    existing = (
        await db.execute(
            select(Instrument.expiry).where(
                Instrument.underlying_instrument_id == underlying.id, Instrument.instrument_type == "option"
            )
        )
    ).scalars().all()
    missing = [expiry for expiry in targets if expiry not in set(existing)]
    if not missing:
        return 0

    queued = 0
    for expiry in missing:
        try:
            ltp = await broker.get_ltp("NSE", underlying.external_ref)
        except KiteAPIError:
            logger.exception("Could not fetch spot price for %s -- skipping expiry %s", underlying.symbol, expiry)
            continue
        spot = ltp["price"]

        expiry_str = expiry.isoformat()
        rows_for_expiry = [
            row for row in nfo_rows
            if row.get("name") == kite_name and row.get("expiry") == expiry_str
            and (row.get("instrument_type") or "").upper() in ("CE", "PE")
            and row.get("strike") not in (None, "", "0")
        ]
        strikes = sorted({float(row["strike"]) for row in rows_for_expiry})
        window = _select_strike_window(strikes, spot, STRIKE_WINDOW)
        if not window:
            logger.warning("No strikes found for %s expiry %s -- skipping", underlying.symbol, expiry)
            continue

        by_strike_and_type = {(float(row["strike"]), (row.get("instrument_type") or "").upper()): row for row in rows_for_expiry}
        for strike in window:
            for option_type in ("CE", "PE"):
                row = by_strike_and_type.get((strike, option_type))
                if row is None:
                    continue
                tradingsymbol = row["tradingsymbol"]
                lot_size_raw = row.get("lot_size")
                symbol_row = await get_or_create_symbol(
                    db, "zerodha_nfo", tradingsymbol, tradingsymbol,
                    expiry=expiry, strike=strike, option_type=option_type,
                    lot_size=int(lot_size_raw) if lot_size_raw else None, underlying_symbol=kite_name,
                )
                job = BfBackfillJob(
                    symbol_id=symbol_row.id, source="zerodha_nfo", timeframe=BACKFILL_TIMEFRAME,
                    requested_by=account_user_id,
                )
                db.add(job)
                # run_bf_backfill_job opens its own AsyncSessionLocal() session
                # to read this row back -- must be committed first, or that
                # fresh session (a different connection) won't see it yet.
                await db.commit()
                await db.refresh(job)
                await run_bf_backfill_job(job.id)
                queued += 1
    return queued


class NfoExpiryRotationScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.last_run_at: datetime | None = None
        self.last_added_count: int = 0
        self.last_error: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None

    async def _run(self) -> None:
        # Acts immediately on start(), sleeps only afterward -- with a 6h
        # interval, a sleep-first loop would leave a fresh deploy waiting
        # 6 hours for its very first check.
        while True:
            try:
                self.last_added_count = await self.check_once()
                self.last_run_at = datetime.now(timezone.utc)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("NFO expiry rotation check failed")
                self.last_error = str(exc)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def check_once(self) -> int:
        async with AsyncSessionLocal() as db:
            account = await find_connected_zerodha_account(db)
            creds = await find_connected_zerodha_credentials(db)
            if account is None or creds is None:
                self.last_error = "No connected Zerodha account"
                return 0

            broker = ZerodhaKiteBroker()
            broker._api_key = creds["api_key"]
            broker._access_token = creds["access_token"]

            total_added = 0
            for underlying in await _tracked_underlyings(db):
                try:
                    total_added += await _ensure_underlying_expiries(db, broker, underlying, account.user_id)
                except KiteAPIError:
                    logger.exception("NFO expiry rotation failed for %s", underlying.symbol)
            return total_added


nfo_expiry_rotation_scheduler = NfoExpiryRotationScheduler()
