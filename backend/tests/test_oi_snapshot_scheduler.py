import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.market_data.oi_snapshot_scheduler import (
    _bucket_start,
    _tracked_option_ids,
    snapshot_once,
)
from app.services.market_data.tick_engine import tick_engine


def test_bucket_start_floors_to_the_15_minute_boundary():
    assert _bucket_start(datetime(2026, 9, 17, 9, 44, 59, tzinfo=timezone.utc)) == datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)
    assert _bucket_start(datetime(2026, 9, 17, 9, 45, 0, tzinfo=timezone.utc)) == datetime(2026, 9, 17, 9, 45, tzinfo=timezone.utc)
    assert _bucket_start(datetime(2026, 9, 17, 9, 0, 1, tzinfo=timezone.utc)) == datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)


async def _seed_underlying(db: AsyncSession) -> Instrument:
    underlying = Instrument(
        exchange="NSE", symbol=f"NIFTY50_{uuid.uuid4().hex[:6]}", name="Nifty 50", instrument_type="index",
        data_source="zerodha_kite", external_ref="NIFTY 50",
    )
    db.add(underlying)
    await db.flush()
    return underlying


def _option(underlying_id, strike, option_type, expiry, data_source="zerodha_kite"):
    symbol = f"OPT{uuid.uuid4().hex[:8]}{option_type}"
    return Instrument(
        exchange="NFO", symbol=symbol, name=symbol, instrument_type="option", data_source=data_source,
        external_ref=symbol, expiry=expiry, strike=float(strike), option_type=option_type, lot_size=65,
        underlying_instrument_id=underlying_id,
    )


async def test_tracked_option_ids_excludes_past_expiry_and_non_nfo_options(db_session: AsyncSession):
    underlying = await _seed_underlying(db_session)
    today = date.today()
    live_ce = _option(underlying.id, 23000, "CE", today + timedelta(days=5))
    expired_pe = _option(underlying.id, 23000, "PE", today - timedelta(days=1))
    other_source = _option(underlying.id, 23100, "CE", today + timedelta(days=5), data_source="yahoo_nse")
    db_session.add_all([live_ce, expired_pe, other_source])
    await db_session.commit()

    tracked = set(await _tracked_option_ids(db_session))
    assert live_ce.id in tracked
    assert expired_pe.id not in tracked
    assert other_source.id not in tracked


async def test_snapshot_once_returns_zero_when_nothing_is_live(db_session: AsyncSession):
    underlying = await _seed_underlying(db_session)
    opt = _option(underlying.id, 23000, "CE", date.today() + timedelta(days=5))
    db_session.add(opt)
    await db_session.commit()

    written = await snapshot_once(db_session)
    assert written == 0
    rows = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == opt.id))).scalars().all()
    assert rows == []


async def test_snapshot_once_inserts_a_new_bar_from_live_tick_data(db_session: AsyncSession):
    underlying = await _seed_underlying(db_session)
    opt = _option(underlying.id, 23000, "CE", date.today() + timedelta(days=5))
    db_session.add(opt)
    await db_session.commit()

    tick_engine.set_real_price(opt.id, 145.5, source="kite")
    tick_engine.set_real_oi(opt.id, 12345.0)

    written = await snapshot_once(db_session)
    assert written == 1

    row = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == opt.id))).scalar_one()
    assert row.timeframe == "15m"
    assert row.open == row.high == row.low == row.close == 145.5
    assert row.open_interest == 12345.0
    assert row.source == "kite_live"


async def test_snapshot_once_skips_a_new_bucket_with_oi_but_no_price(db_session: AsyncSession):
    """A contract can have OI on file with no price yet on the rare tick
    that carries one but not the other -- can't fabricate open/high/low
    from nothing, so this contract is just skipped this cycle rather than
    writing a bogus bar."""
    underlying = await _seed_underlying(db_session)
    opt = _option(underlying.id, 23000, "CE", date.today() + timedelta(days=5))
    db_session.add(opt)
    await db_session.commit()

    tick_engine.set_real_oi(opt.id, 500.0)  # no set_real_price call

    written = await snapshot_once(db_session)
    assert written == 0
    rows = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == opt.id))).scalars().all()
    assert rows == []


async def test_snapshot_once_updates_the_existing_bucket_in_place(db_session: AsyncSession):
    underlying = await _seed_underlying(db_session)
    opt = _option(underlying.id, 23000, "CE", date.today() + timedelta(days=5))
    db_session.add(opt)
    await db_session.flush()

    from app.services.market_data.oi_snapshot_scheduler import _bucket_start

    bucket_ts = _bucket_start(datetime.now(timezone.utc))
    db_session.add(
        OhlcvCandle(
            instrument_id=opt.id, timeframe="15m", ts=bucket_ts,
            open=100.0, high=105.0, low=98.0, close=102.0, open_interest=1000.0, source="kite_live",
        )
    )
    await db_session.commit()

    tick_engine.set_real_price(opt.id, 110.0, source="kite")  # new high
    tick_engine.set_real_oi(opt.id, 1500.0)

    written = await snapshot_once(db_session)
    assert written == 1

    row = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == opt.id))).scalar_one()
    assert row.close == 110.0
    assert row.high == 110.0  # widened to the new tick
    assert row.low == 98.0  # unchanged -- new tick wasn't a new low
    assert row.open == 100.0  # unchanged -- open is fixed once a bucket exists
    assert row.open_interest == 1500.0


async def test_snapshot_once_updates_oi_only_when_the_existing_bucket_has_no_new_price(db_session: AsyncSession):
    underlying = await _seed_underlying(db_session)
    opt = _option(underlying.id, 23000, "CE", date.today() + timedelta(days=5))
    db_session.add(opt)
    await db_session.flush()

    from app.services.market_data.oi_snapshot_scheduler import _bucket_start

    bucket_ts = _bucket_start(datetime.now(timezone.utc))
    db_session.add(
        OhlcvCandle(
            instrument_id=opt.id, timeframe="15m", ts=bucket_ts,
            open=100.0, high=105.0, low=98.0, close=102.0, open_interest=1000.0, source="kite_live",
        )
    )
    await db_session.commit()

    # A brand-new, never-before-seen instrument (no set_real_price call
    # anywhere) reports OI-only -- get_current_price returns None for it.
    tick_engine.set_real_oi(opt.id, 2000.0)

    written = await snapshot_once(db_session)
    assert written == 1

    row = (await db_session.execute(select(OhlcvCandle).where(OhlcvCandle.instrument_id == opt.id))).scalar_one()
    assert row.open_interest == 2000.0
    assert row.close == 102.0  # unchanged, no live price this cycle
