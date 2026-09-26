"""The open interest the F&O opening-momentum scan compares.

The scan's Total OI for a stock is its current-month future plus every CE
and PE strike of that same expiry. Today's side is read live at the scan;
yesterday's side is the close captured here at 15:31 the day before
(models/fo_scan.py), so nothing needs downloading at 09:20.

Captures (oi_store_scheduler.py):
  close     15:31, every F&O stock -- tomorrow's "yesterday". On a stock's
            expiry day its next month is captured too: that's the
            current month from tomorrow.
  pre_open  09:10, only when yesterday's close is missing (e.g. no
            Zerodha login that afternoon). Before the open, Kite's quote
            still shows the previous session's closing OI.
  09:20     every F&O stock, for the permanent per-stock totals.

Each is one pass of Kite /quote calls, 500 contracts a request (~12,300
contracts: 25 requests, about 30 seconds). Per-contract rows are kept for
the last two sessions only (prune); per-stock totals are kept for good.

If both the close and the pre-open reading are missing, the strategy can
fall back to Kite's daily candles for the few stocks it needs
(save_daily_candle_baseline) -- ~58 history requests a stock, so only for
stocks that already moved more than 2%.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import delete, exists, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.fo_scan import MARK_CLOSE, MARK_PRE_OPEN, FoOiSnapshot, FoOiTotal
from app.models.instrument import Instrument
from app.services.backfill_platform.coverage import IST, previous_trading_day
from app.services.broker.zerodha_broker import KiteAPIError, ZerodhaKiteBroker
from app.services.fo_scan.pacing import kite_history, kite_quotes

SOURCE_QUOTE = "quote"
SOURCE_DAILY_CANDLE = "daily_candle"
BASELINE_CLOSE = "close"
BASELINE_PRE_OPEN = "pre_open"
BASELINE_DAILY_CANDLE = "daily_candle"
WRITE_CHUNK = 1000


@dataclass(frozen=True)
class Contract:
    instrument_id: uuid.UUID
    underlying_id: uuid.UUID
    symbol: str  # the stock
    kind: str  # FUT | CE | PE
    expiry: date
    strike: float | None
    exchange: str
    tradingsymbol: str

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"


async def stock_futures(db: AsyncSession, session: date) -> dict[uuid.UUID, list[Instrument]]:
    """Each F&O stock's unexpired futures, nearest first. Index futures
    have no underlying equity and are left out."""
    rows = (
        await db.execute(
            select(Instrument)
            .where(
                Instrument.instrument_type == "future", Instrument.underlying_instrument_id.is_not(None),
                Instrument.expiry.is_not(None), Instrument.expiry >= session,
            )
            .order_by(Instrument.expiry)
        )
    ).scalars().all()
    by_stock: dict[uuid.UUID, list[Instrument]] = defaultdict(list)
    for fut in rows:
        by_stock[fut.underlying_instrument_id].append(fut)
    return dict(by_stock)


def _kind(instrument: Instrument) -> str:
    return "FUT" if instrument.instrument_type == "future" else instrument.option_type


def _contract(instrument: Instrument, underlying_id: uuid.UUID, symbol: str) -> Contract:
    return Contract(
        instrument_id=instrument.id, underlying_id=underlying_id, symbol=symbol, kind=_kind(instrument),
        expiry=instrument.expiry, strike=instrument.strike, exchange=instrument.exchange, tradingsymbol=instrument.external_ref,
    )


async def stock_contracts(
    db: AsyncSession, session: date, *, underlying_ids: set[uuid.UUID] | None = None, next_month_on_expiry: bool = False,
) -> list[Contract]:
    """Every contract in the Total OI of each F&O stock (or just
    `underlying_ids`): the current-month future and all CE/PE strikes of
    that expiry. `next_month_on_expiry`: on a stock's expiry day, its next
    month too."""
    futures = await stock_futures(db, session)
    if underlying_ids is not None:
        futures = {uid: futs for uid, futs in futures.items() if uid in underlying_ids}
    if not futures:
        return []
    symbols = dict(
        (await db.execute(select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(list(futures))))).all()
    )
    wanted: dict[uuid.UUID, set[date]] = {}
    contracts: list[Contract] = []
    for uid, futs in futures.items():
        if uid not in symbols:
            continue
        chosen = [futs[0]]
        if next_month_on_expiry and futs[0].expiry == session and len(futs) > 1:
            chosen.append(futs[1])
        wanted[uid] = {f.expiry for f in chosen}
        contracts.extend(_contract(f, uid, symbols[uid]) for f in chosen)
    all_expiries = set().union(*wanted.values()) if wanted else set()
    ids = list(wanted)
    for i in range(0, len(ids), WRITE_CHUNK):
        options = (
            await db.execute(
                select(Instrument).where(
                    Instrument.instrument_type == "option", Instrument.underlying_instrument_id.in_(ids[i : i + WRITE_CHUNK]),
                    Instrument.expiry.in_(all_expiries), Instrument.strike.is_not(None), Instrument.option_type.in_(("CE", "PE")),
                )
            )
        ).scalars().all()
        for opt in options:
            if opt.expiry in wanted[opt.underlying_instrument_id]:
                contracts.append(_contract(opt, opt.underlying_instrument_id, symbols[opt.underlying_instrument_id]))
    return contracts


async def write_readings(
    db: AsyncSession, session: date, mark: str, contracts: list[Contract], readings: dict[uuid.UUID, dict],
    captured_at: datetime, source: str = SOURCE_QUOTE,
) -> None:
    """Replaces the (session, mark) rows of `contracts` with `readings`
    ({instrument_id: {"oi", "volume", "last_price"}}) and rewrites the
    per-stock totals of the stocks they belong to. Doesn't commit."""
    ids = [c.instrument_id for c in contracts]
    for i in range(0, len(ids), WRITE_CHUNK):
        await db.execute(
            delete(FoOiSnapshot)
            .where(FoOiSnapshot.session_date == session, FoOiSnapshot.mark == mark, FoOiSnapshot.instrument_id.in_(ids[i : i + WRITE_CHUNK]))
            .execution_options(synchronize_session=False)
        )
    rows = [
        {
            "id": uuid.uuid4(), "session_date": session, "mark": mark, "underlying_id": c.underlying_id,
            "instrument_id": c.instrument_id, "kind": c.kind, "expiry": c.expiry, "strike": c.strike,
            "oi": readings[c.instrument_id].get("oi"), "volume": readings[c.instrument_id].get("volume"),
            "last_price": readings[c.instrument_id].get("last_price"), "source": source, "captured_at": captured_at,
        }
        for c in contracts if c.instrument_id in readings
    ]
    for i in range(0, len(rows), WRITE_CHUNK):
        await db.execute(insert(FoOiSnapshot), rows[i : i + WRITE_CHUNK])

    groups: dict[tuple, list[Contract]] = defaultdict(list)
    for c in contracts:
        groups[(c.underlying_id, c.symbol, c.expiry)].append(c)
    symbols = sorted({symbol for _, symbol, _ in groups})
    for i in range(0, len(symbols), WRITE_CHUNK):
        await db.execute(
            delete(FoOiTotal)
            .where(FoOiTotal.session_date == session, FoOiTotal.mark == mark, FoOiTotal.symbol.in_(symbols[i : i + WRITE_CHUNK]))
            .execution_options(synchronize_session=False)
        )
    totals = []
    for (uid, symbol, expiry), legs in groups.items():
        sums = {"FUT": None, "CE": None, "PE": None}
        with_oi = 0
        for c in legs:
            oi = (readings.get(c.instrument_id) or {}).get("oi")
            if oi is None:
                continue
            with_oi += 1
            sums[c.kind] = (sums[c.kind] or 0.0) + float(oi)
        known = [v for v in sums.values() if v is not None]
        totals.append({
            "id": uuid.uuid4(), "session_date": session, "mark": mark, "symbol": symbol, "underlying_id": uid, "expiry": expiry,
            "fut_oi": sums["FUT"], "ce_oi": sums["CE"], "pe_oi": sums["PE"], "total_oi": sum(known) if known else None,
            "contracts_listed": len(legs), "contracts_with_oi": with_oi, "captured_at": captured_at,
        })
    for i in range(0, len(totals), WRITE_CHUNK):
        await db.execute(insert(FoOiTotal), totals[i : i + WRITE_CHUNK])


async def capture(
    db: AsyncSession, broker: ZerodhaKiteBroker, session: date, mark: str, contracts: list[Contract], now: datetime | None = None,
) -> dict:
    """One live reading of `contracts` from Kite /quote, saved as (session,
    mark). Commits."""
    quotes, error = await kite_quotes(broker, [c.key for c in contracts])
    readings = {}
    for c in contracts:
        q = quotes.get(c.key)
        if q is not None:
            readings[c.instrument_id] = {"oi": q.get("oi"), "volume": q.get("volume"), "last_price": q.get("last_price")}
    captured_at = now or datetime.now(timezone.utc)
    await write_readings(db, session, mark, contracts, readings, captured_at)
    await db.commit()
    return {
        "contracts": len(contracts), "quoted": len(readings),
        "with_oi": sum(1 for r in readings.values() if r.get("oi") is not None), "error": error,
    }


async def prune(db: AsyncSession, today: date) -> int:
    """Drops per-contract readings older than the previous session: the
    scan only ever compares today with yesterday's close. Commits."""
    keep_from = previous_trading_day(today)
    result = await db.execute(
        delete(FoOiSnapshot).where(FoOiSnapshot.session_date < keep_from).execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0


async def has_reading(db: AsyncSession, session: date, mark: str, source: str = SOURCE_QUOTE) -> bool:
    return bool(
        (
            await db.execute(
                select(exists().where(FoOiSnapshot.session_date == session, FoOiSnapshot.mark == mark, FoOiSnapshot.source == source))
            )
        ).scalar()
    )


async def previous_close(db: AsyncSession, session: date, instrument_ids: list[uuid.UUID]) -> tuple[dict[uuid.UUID, float], str | None]:
    """Yesterday's closing OI of `instrument_ids` -- ({instrument_id: oi},
    where it came from) from the previous session's close, else today's
    pre-open backup, else ({}, None)."""
    prev = previous_trading_day(session)
    for day, mark in ((prev, MARK_CLOSE), (session, MARK_PRE_OPEN)):
        rows = (
            await db.execute(
                select(FoOiSnapshot.instrument_id, FoOiSnapshot.oi, FoOiSnapshot.source).where(
                    FoOiSnapshot.session_date == day, FoOiSnapshot.mark == mark,
                    FoOiSnapshot.instrument_id.in_(instrument_ids), FoOiSnapshot.oi.is_not(None),
                )
            )
        ).all()
        if rows:
            if mark == MARK_PRE_OPEN:
                label = BASELINE_PRE_OPEN
            elif all(r.source == SOURCE_DAILY_CANDLE for r in rows):
                label = BASELINE_DAILY_CANDLE
            else:
                label = BASELINE_CLOSE
            return {r.instrument_id: float(r.oi) for r in rows}, label
    return {}, None


async def save_daily_candle_baseline(
    db: AsyncSession, broker: ZerodhaKiteBroker, session: date, contracts: list[Contract],
) -> dict[uuid.UUID, float]:
    """Last resort when neither yesterday's close nor today's pre-open was
    captured: each contract's OI from Kite's daily candles, as of the
    previous session (OI only changes on a trade, so a contract that didn't
    trade then keeps its last traded day's OI). One history request per
    contract. Saved as the previous session's close. Commits."""
    prev = previous_trading_day(session)
    start = datetime.combine(prev - timedelta(days=10), time(0, 0), tzinfo=IST)
    end = datetime.combine(session, time(0, 0), tzinfo=IST)
    readings: dict[uuid.UUID, dict] = {}
    for c in contracts:
        try:
            bars = await kite_history(broker, c.tradingsymbol, "1d", start, end, c.exchange)
        except KiteAPIError:
            continue
        known = [b for b in bars if as_aware_utc(b["ts"]).astimezone(IST).date() <= prev and b.get("open_interest") is not None]
        if known:
            readings[c.instrument_id] = {"oi": known[-1]["open_interest"], "volume": None, "last_price": known[-1].get("close")}
    await write_readings(db, prev, MARK_CLOSE, contracts, readings, datetime.now(timezone.utc), source=SOURCE_DAILY_CANDLE)
    await db.commit()
    return {uid: float(r["oi"]) for uid, r in readings.items()}
