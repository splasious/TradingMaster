from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backtest.candle_source import load_candles


async def _seed_daily_candles(db_session: AsyncSession, symbol: str, n_days: int, start: datetime) -> Instrument:
    instrument = Instrument(exchange="NSE", symbol=symbol, name=symbol, instrument_type="equity", data_source="yahoo_nse", external_ref=symbol)
    db_session.add(instrument)
    await db_session.flush()
    for i in range(n_days):
        db_session.add(OhlcvCandle(
            instrument_id=instrument.id, timeframe="1d", ts=start + timedelta(days=i),
            open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=1000.0, source="test",
        ))
    await db_session.commit()
    return instrument


async def test_load_candles_returns_direct_match_when_stored(db_session: AsyncSession):
    instrument = await _seed_daily_candles(db_session, "LOADDIRECT", 40, datetime(2026, 1, 5, tzinfo=timezone.utc))
    candles = await load_candles(db_session, instrument.id, "1d")
    assert len(candles) == 40
    assert all(c.timeframe == "1d" for c in candles)


async def test_load_candles_resamples_when_target_not_directly_stored(db_session: AsyncSession):
    # Well in the past, so every resampled week is fully closed.
    instrument = await _seed_daily_candles(db_session, "LOADRESAMPLE", 21, datetime(2026, 1, 5, tzinfo=timezone.utc))
    candles = await load_candles(db_session, instrument.id, "1wk")
    assert len(candles) > 0
    assert all(c.timeframe == "1wk" for c in candles)
    assert all(c.source == "resampled" for c in candles)


async def test_load_candles_returns_empty_when_no_usable_base(db_session: AsyncSession):
    instrument = Instrument(exchange="NSE", symbol="LOADNONE", name="Load None", instrument_type="equity", data_source="yahoo_nse", external_ref="LOADNONE")
    db_session.add(instrument)
    await db_session.commit()
    candles = await load_candles(db_session, instrument.id, "1wk")
    assert candles == []


async def test_load_candles_respects_date_range_on_resampled_path(db_session: AsyncSession):
    instrument = await _seed_daily_candles(db_session, "LOADRANGE", 60, datetime(2026, 1, 5, tzinfo=timezone.utc))
    all_weeks = await load_candles(db_session, instrument.id, "1wk")
    limited = await load_candles(db_session, instrument.id, "1wk", start_date=datetime(2026, 1, 5).date(), end_date=datetime(2026, 1, 25).date())
    assert len(limited) < len(all_weeks)
    assert len(limited) > 0
