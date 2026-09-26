"""1-minute is gone: not offered, not in the top-up, and purge.py deletes
its saved candles from both tables and bf_coverage, leaving every other
timeframe alone."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.backfill_platform import DEFAULT_TOPUP_TIMEFRAMES, BfCoverage, BfOhlcvBar, BfSettings, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform import purge
from app.services.backfill_platform.overview import TIMEFRAMES
from app.services.backfill_platform.timeframes import timeframes_for_source

T0 = datetime(2026, 9, 25, 3, 45, tzinfo=timezone.utc)


def test_one_minute_is_no_longer_offered():
    for source in ("zerodha", "zerodha_nfo", "delta"):
        assert "1m" not in {o.value for o in timeframes_for_source(source)}
    assert "1m" not in DEFAULT_TOPUP_TIMEFRAMES
    assert "1m" not in TIMEFRAMES


async def test_purge_deletes_only_the_listed_timeframe(db_engine, db_session: AsyncSession, monkeypatch):
    monkeypatch.setattr(purge, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))
    monkeypatch.setattr(purge, "PAUSE_SECONDS", 0)
    db_session.add(BfSettings(id=1, purge_timeframes=["1m"]))
    for name in ("AAA", "BBB"):
        sym = BfSymbol(source="zerodha", symbol=name, display_name=name)
        inst = Instrument(exchange="NSE", symbol=name, name=name, instrument_type="equity", data_source="zerodha_kite", external_ref=name)
        db_session.add_all([sym, inst])
        await db_session.flush()
        for tf, step, n in (("1m", 1, 30), ("5m", 5, 6)):
            for i in range(n):
                ts = T0 + timedelta(minutes=step * i)
                db_session.add(BfOhlcvBar(symbol_id=sym.id, timeframe=tf, ts=ts, open=1, high=1, low=1, close=1))
                db_session.add(OhlcvCandle(instrument_id=inst.id, timeframe=tf, ts=ts, open=1, high=1, low=1, close=1, source="kite"))
            db_session.add(BfCoverage(symbol_id=sym.id, timeframe=tf, first_ts=T0, last_ts=T0, bar_count=n))
    await db_session.commit()

    job = purge.TimeframePurge()
    await job.run()

    async def count(model, tf):
        return (await db_session.execute(select(func.count()).select_from(model).where(model.timeframe == tf))).scalar_one()

    assert (await count(BfOhlcvBar, "1m"), await count(OhlcvCandle, "1m"), await count(BfCoverage, "1m")) == (0, 0, 0)
    assert (await count(BfOhlcvBar, "5m"), await count(OhlcvCandle, "5m"), await count(BfCoverage, "5m")) == (12, 12, 2)
    assert (job.deleted_backfill_bars, job.deleted_chart_candles, job.last_error) == (60, 60, None)
    await db_session.refresh(await db_session.get(BfSettings, 1))
    assert (await db_session.get(BfSettings, 1)).purge_timeframes == []

    # Nothing listed: a no-op.
    again = purge.TimeframePurge()
    await again.run()
    assert again.started_at is None


async def test_purge_deletes_stock_options_candles_only(db_engine, db_session: AsyncSession, monkeypatch):
    """Stock options' candles go from both tables and bf_coverage; index
    options, futures and the contracts themselves stay."""
    monkeypatch.setattr(purge, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))
    monkeypatch.setattr(purge, "PAUSE_SECONDS", 0)
    db_session.add(BfSettings(id=1, purge_stock_options=True))
    contracts = {
        "RELIANCE26SEP1400CE": ("RELIANCE", "CE"), "RELIANCE26SEP1300PE": ("RELIANCE", "PE"),
        "NIFTY26SEP25000CE": ("NIFTY", "CE"), "RELIANCE26SEPFUT": ("RELIANCE", None),
    }
    for name, (underlying, option_type) in contracts.items():
        sym = BfSymbol(source="zerodha_nfo", symbol=name, display_name=name, underlying_symbol=underlying, option_type=option_type)
        inst = Instrument(exchange="NFO", symbol=name, name=name, instrument_type="option" if option_type else "future",
                          data_source="zerodha_kite", external_ref=name)
        db_session.add_all([sym, inst])
        await db_session.flush()
        for i in range(4):
            ts = T0 + timedelta(minutes=15 * i)
            db_session.add(BfOhlcvBar(symbol_id=sym.id, timeframe="15m", ts=ts, open=1, high=1, low=1, close=1))
            db_session.add(OhlcvCandle(instrument_id=inst.id, timeframe="15m", ts=ts, open=1, high=1, low=1, close=1, source="kite"))
        db_session.add(BfCoverage(symbol_id=sym.id, timeframe="15m", first_ts=T0, last_ts=T0, bar_count=4))
    await db_session.commit()

    job = purge.TimeframePurge()
    await job.run()
    assert (job.deleted_backfill_bars, job.deleted_chart_candles, job.last_error) == (8, 8, None)

    kept = (await db_session.execute(select(BfSymbol.symbol).join(BfOhlcvBar, BfOhlcvBar.symbol_id == BfSymbol.id).distinct())).scalars().all()
    assert sorted(kept) == ["NIFTY26SEP25000CE", "RELIANCE26SEPFUT"]
    assert (await db_session.execute(select(func.count()).select_from(OhlcvCandle))).scalar_one() == 8
    assert (await db_session.execute(select(func.count()).select_from(BfCoverage))).scalar_one() == 2
    assert (await db_session.execute(select(func.count()).select_from(BfSymbol))).scalar_one() == 4  # contracts kept
    await db_session.refresh(await db_session.get(BfSettings, 1))
    assert (await db_session.get(BfSettings, 1)).purge_stock_options is False
