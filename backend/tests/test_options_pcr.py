import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.options.pcr import compute_effective_pcr, compute_pcr_series

EXPIRY = date(2026, 9, 15)


async def _make_option(db: AsyncSession, underlying_id, symbol: str, option_type: str, strike: float) -> Instrument:
    inst = Instrument(
        exchange="NFO", symbol=symbol, name=symbol, instrument_type="option", data_source="zerodha_kite",
        external_ref=symbol, expiry=EXPIRY, strike=strike, option_type=option_type, lot_size=65,
        underlying_instrument_id=underlying_id,
    )
    db.add(inst)
    await db.flush()
    return inst


async def _add_candle(db: AsyncSession, instrument_id, ts: datetime, oi: float | None) -> None:
    db.add(OhlcvCandle(
        instrument_id=instrument_id, timeframe="15m", ts=ts, open=100, high=101, low=99, close=100,
        volume=10, open_interest=oi, source="test",
    ))


async def test_pcr_aggregates_across_strikes_at_each_timestamp(db_session: AsyncSession):
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.flush()

    ce_a = await _make_option(db_session, underlying.id, "NIFTY26SEP23000CE", "CE", 23000)
    ce_b = await _make_option(db_session, underlying.id, "NIFTY26SEP23100CE", "CE", 23100)
    pe_a = await _make_option(db_session, underlying.id, "NIFTY26SEP23000PE", "PE", 23000)

    t0 = datetime(2026, 9, 1, 9, 15, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=15)

    # t0: total call OI = 1000+500=1500, total put OI = 3000 -> PCR = 2.0
    await _add_candle(db_session, ce_a.id, t0, 1000)
    await _add_candle(db_session, ce_b.id, t0, 500)
    await _add_candle(db_session, pe_a.id, t0, 3000)
    # t1: call OI rises to 1800 (2000+... wait keep simple), put OI falls to 2400
    await _add_candle(db_session, ce_a.id, t1, 1200)
    await _add_candle(db_session, ce_b.id, t1, 600)
    await _add_candle(db_session, pe_a.id, t1, 2400)
    await db_session.commit()

    series = await compute_pcr_series(db_session, underlying.id, EXPIRY, "15m")

    assert len(series) == 2
    assert series[0]["total_call_oi"] == 1500
    assert series[0]["total_put_oi"] == 3000
    assert series[0]["pcr"] == 2.0
    assert series[0]["call_oi_change"] is None  # no prior bar
    assert series[0]["put_oi_change"] is None

    assert series[1]["total_call_oi"] == 1800
    assert series[1]["total_put_oi"] == 2400
    assert series[1]["pcr"] == 2400 / 1800
    assert series[1]["call_oi_change"] == 300  # 1800 - 1500
    assert series[1]["put_oi_change"] == -600  # 2400 - 3000


async def test_pcr_none_when_call_oi_zero(db_session: AsyncSession):
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.flush()
    ce = await _make_option(db_session, underlying.id, "NIFTY26SEP23000CE", "CE", 23000)
    pe = await _make_option(db_session, underlying.id, "NIFTY26SEP23000PE", "PE", 23000)
    t0 = datetime(2026, 9, 1, 9, 15, tzinfo=timezone.utc)
    await _add_candle(db_session, ce.id, t0, 0)
    await _add_candle(db_session, pe.id, t0, 500)
    await db_session.commit()

    series = await compute_pcr_series(db_session, underlying.id, EXPIRY, "15m")
    assert series[0]["pcr"] is None  # undefined, not zero or infinity


async def test_pcr_ignores_candles_with_no_open_interest(db_session: AsyncSession):
    """A bar with open_interest=None (e.g. equity/crypto candle shape, or a
    genuinely missing OI reading) must not be silently counted as zero --
    it's excluded from the aggregation entirely."""
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.flush()
    ce = await _make_option(db_session, underlying.id, "NIFTY26SEP23000CE", "CE", 23000)
    t0 = datetime(2026, 9, 1, 9, 15, tzinfo=timezone.utc)
    await _add_candle(db_session, ce.id, t0, None)
    await db_session.commit()

    series = await compute_pcr_series(db_session, underlying.id, EXPIRY, "15m")
    assert series == []  # the only candle had no OI, so no timestamp is populated at all


async def test_pcr_empty_when_no_options_for_expiry(db_session: AsyncSession):
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.commit()

    series = await compute_pcr_series(db_session, underlying.id, EXPIRY, "15m")
    assert series == []


async def test_compute_effective_pcr_sums_oi_across_nearest_4_expiries(db_session: AsyncSession):
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.flush()

    expiries = [date(2026, 9, 17), date(2026, 9, 24), date(2026, 10, 1), date(2026, 10, 8)]
    t0 = datetime(2026, 9, 12, 9, 15, tzinfo=timezone.utc)
    for i, expiry in enumerate(expiries):
        ce = Instrument(
            exchange="NFO", symbol=f"NIFTY_CE_{i}", name=f"NIFTY_CE_{i}", instrument_type="option",
            data_source="zerodha_kite", external_ref=f"NIFTY_CE_{i}", expiry=expiry, strike=23000, option_type="CE",
            lot_size=65, underlying_instrument_id=underlying.id,
        )
        pe = Instrument(
            exchange="NFO", symbol=f"NIFTY_PE_{i}", name=f"NIFTY_PE_{i}", instrument_type="option",
            data_source="zerodha_kite", external_ref=f"NIFTY_PE_{i}", expiry=expiry, strike=23000, option_type="PE",
            lot_size=65, underlying_instrument_id=underlying.id,
        )
        db_session.add_all([ce, pe])
        await db_session.flush()
        # 100/50 call OI and 150/50 put OI per expiry -> totals 600 call, 800 put -> PCR = 800/600
        await _add_candle(db_session, ce.id, t0, 100.0 + i)
        await _add_candle(db_session, pe.id, t0, 150.0 + i)
    await db_session.commit()

    pcr = await compute_effective_pcr(db_session, underlying_symbol="NIFTY 50", num_expiries=4, timeframe="15m")
    expected_call = sum(100.0 + i for i in range(4))
    expected_put = sum(150.0 + i for i in range(4))
    assert pcr == expected_put / expected_call


async def test_compute_effective_pcr_only_uses_nearest_expiries_beyond_limit(db_session: AsyncSession):
    """A 5th, farther-out expiry must not be included when num_expiries=4."""
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.flush()

    expiries = [date(2026, 9, 17), date(2026, 9, 24), date(2026, 10, 1), date(2026, 10, 8), date(2026, 10, 15)]
    t0 = datetime(2026, 9, 12, 9, 15, tzinfo=timezone.utc)
    for i, expiry in enumerate(expiries):
        ce = Instrument(
            exchange="NFO", symbol=f"NIFTY_CE_{i}", name=f"NIFTY_CE_{i}", instrument_type="option",
            data_source="zerodha_kite", external_ref=f"NIFTY_CE_{i}", expiry=expiry, strike=23000, option_type="CE",
            lot_size=65, underlying_instrument_id=underlying.id,
        )
        pe = Instrument(
            exchange="NFO", symbol=f"NIFTY_PE_{i}", name=f"NIFTY_PE_{i}", instrument_type="option",
            data_source="zerodha_kite", external_ref=f"NIFTY_PE_{i}", expiry=expiry, strike=23000, option_type="PE",
            lot_size=65, underlying_instrument_id=underlying.id,
        )
        db_session.add_all([ce, pe])
        await db_session.flush()
        is_farthest = i == 4
        # The 5th (farthest) expiry's OI is wildly skewed (call=100,
        # put=100000) -- if compute_effective_pcr's num_expiries=4 limit
        # didn't actually exclude it, PCR would blow way past 1.0.
        await _add_candle(db_session, ce.id, t0, 100.0)
        await _add_candle(db_session, pe.id, t0, 100000.0 if is_farthest else 100.0)
    await db_session.commit()

    pcr = await compute_effective_pcr(db_session, underlying_symbol="NIFTY 50", num_expiries=4, timeframe="15m")
    assert pcr == 1.0  # only the nearest 4 (100/100 each) counted, 5th's skew excluded


async def test_compute_effective_pcr_none_when_underlying_not_found(db_session: AsyncSession):
    pcr = await compute_effective_pcr(db_session, underlying_symbol="NOT_A_REAL_SYMBOL")
    assert pcr is None


async def test_compute_effective_pcr_none_when_no_upcoming_expiries(db_session: AsyncSession):
    underlying = Instrument(exchange="NSE", symbol="NIFTY 50", name="Nifty 50 Index", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50")
    db_session.add(underlying)
    await db_session.commit()

    pcr = await compute_effective_pcr(db_session, underlying_symbol="NIFTY 50")
    assert pcr is None
