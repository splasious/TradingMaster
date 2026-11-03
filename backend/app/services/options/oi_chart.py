"""The PCR Analysis tab's live NIFTY 50 chart (3 Nov): the Telegram OI-levels
picture (oi_levels.py) on the page, live -- NIFTY 50's 15-minute candles and
the one forming, with an expiry's top three call OI strikes above NIFTY and
top three put OI strikes below it as bars at their prices.

This hands the page what it starts from; the page then moves it tick by
tick over the market-data WebSocket (the Kite ticker streams NIFTY and its
options of the nearest expiries, open interest included):
  - NIFTY 50's last CANDLES finished 15-minute candles and the one forming
    (notifications/candle_panel.py, as the picture builds them);
  - the latest PCR record -- its time, NIFTY level and PCR;
  - every contract of the chosen expiry the record captured (ATM ±60
    strikes, pcr_snapshots.py): strike, CE/PE, the app's instrument id when
    it has one (what the page subscribes to for live OI), and its OI at the
    record, at the record before it and at the previous session's last
    record.
The page ranks the top three each side itself, from live OI where it has
it, so a level that changes hands shows at once. Reads the database and
the tick engine only -- no Kite call.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.pcr import PcrSnapshot, PcrStrikeOi
from app.services.market_data.tick_engine import tick_engine
from app.services.notifications import candle_panel
from app.services.options.pcr_live import market_open
from app.services.options.pcr_snapshots import LOW_COVERAGE, UNDERLYINGS

CANDLES = 50  # about two sessions of 15-minute candles
LIVE_PRICE_AGE = timedelta(minutes=2)  # an older NIFTY tick isn't "now"
RECORDS_TO_TRY = 5  # newest records looked at for one with enough contracts


@dataclass
class ChartContract:
    strike: float
    option_type: str  # CE / PE
    instrument_id: str | None  # None when the app has no instrument for it: no live OI then
    oi: float | None  # at the record
    prev_oi: float | None  # at the record before it
    day_oi: float | None  # at the previous session's last record


async def _record(db: AsyncSession, underlying: str, now: datetime) -> PcrSnapshot | None:
    """The newest record with a NIFTY level, expiries and enough contracts
    -- today's, or the last session's when today has none yet."""
    snaps = (
        await db.execute(
            select(PcrSnapshot)
            .where(PcrSnapshot.underlying == underlying, PcrSnapshot.ts <= now, PcrSnapshot.spot.is_not(None))
            .order_by(PcrSnapshot.ts.desc()).limit(RECORDS_TO_TRY)
        )
    ).scalars().all()
    for snap in snaps:
        if snap.expiries and snap.contracts_expected and snap.contracts_with_oi >= LOW_COVERAGE * snap.contracts_expected:
            return snap
    return None


async def _oi_at(db: AsyncSession, underlying: str, ts: datetime | None, expiry: date) -> dict[tuple[float, str], float | None]:
    if ts is None:
        return {}
    rows = (
        await db.execute(
            select(PcrStrikeOi.strike, PcrStrikeOi.option_type, PcrStrikeOi.oi)
            .join(PcrSnapshot, PcrSnapshot.id == PcrStrikeOi.snapshot_id)
            .where(PcrSnapshot.underlying == underlying, PcrSnapshot.ts == ts, PcrStrikeOi.expiry == expiry)
        )
    ).all()
    return {(strike, option_type): oi for strike, option_type, oi in rows}


async def oi_chart(db: AsyncSession, underlying: str, expiry: date | None = None, now: datetime | None = None) -> dict:
    now = as_aware_utc(now or datetime.now(timezone.utc))
    is_open = market_open(now)
    nifty = (
        await db.execute(select(Instrument).where(Instrument.symbol == UNDERLYINGS[underlying], Instrument.exchange == "NSE"))
    ).scalars().first()
    live = tick_engine.get_fresh_real_price(nifty.id, LIVE_PRICE_AGE, now) if nifty is not None and is_open else None
    recent = await candle_panel.load_recent(db, nifty.id, now, live, n=CANDLES) if nifty is not None else None

    snap = await _record(db, underlying, now)
    expiries = [date.fromisoformat(e) for e in snap.expiries] if snap is not None else []
    chosen = expiry if expiry in expiries else (expiries[0] if expiries else None)
    contracts: list[ChartContract] = []
    if snap is not None and chosen is not None:
        rows = (
            await db.execute(
                select(PcrStrikeOi.strike, PcrStrikeOi.option_type, PcrStrikeOi.tradingsymbol, PcrStrikeOi.oi)
                .where(PcrStrikeOi.snapshot_id == snap.id, PcrStrikeOi.expiry == chosen)
                .order_by(PcrStrikeOi.strike, PcrStrikeOi.option_type)
            )
        ).all()
        ids = dict(
            (
                await db.execute(
                    select(Instrument.external_ref, Instrument.id).where(
                        Instrument.exchange == "NFO", Instrument.data_source == "zerodha_kite",
                        Instrument.external_ref.in_([symbol for _, _, symbol, _ in rows]),
                    )
                )
            ).all()
        )
        prev = await _oi_at(db, underlying, snap.prev_ts, chosen)
        day = await _oi_at(db, underlying, snap.day_baseline_ts, chosen)
        contracts = [
            ChartContract(strike, option_type, str(ids[symbol]) if symbol in ids else None, oi,
                          prev.get((strike, option_type)), day.get((strike, option_type)))
            for strike, option_type, symbol, oi in rows
        ]

    def candle(bar: dict) -> dict:
        return {k: bar[k] for k in ("ts", "open", "high", "low", "close")}

    return {
        "underlying": underlying,
        "nifty_instrument_id": str(nifty.id) if nifty is not None else None,
        "market_open": is_open,
        "live_price": live,
        "candles": [candle(b) for b in recent.candles] if recent is not None else [],
        "forming": candle(recent.forming) if recent is not None and recent.forming else None,
        "record_ts": as_aware_utc(snap.ts) if snap is not None else None,
        "record_spot": snap.spot if snap is not None else None,
        "record_pcr": snap.pcr if snap is not None else None,
        "prev_ts": as_aware_utc(snap.prev_ts) if snap is not None and snap.prev_ts else None,
        "strike_step": snap.strike_step if snap is not None else None,
        "expiry": chosen,
        "expiries": expiries,
        "contracts": [c.__dict__ for c in contracts],
    }
