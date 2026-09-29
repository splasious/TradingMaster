"""The evening copy (catalog_sync.sync_symbol_to_catalog) replaces a chart
candle of the last REPLACE_DAYS that differs from the backfill copy -- Kite's
final candle -- such as a live open-interest snapshot or a candle saved
before it finished; a matching or older one is left alone."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.backfill_platform import BfOhlcvBar, BfSymbol
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.backfill_platform import catalog_sync

TODAY_0930 = datetime.now(timezone.utc).replace(hour=4, minute=0, second=0, microsecond=0)  # 09:30 IST


async def _pair(db, source: str, symbol: str) -> tuple[BfSymbol, Instrument]:
    bf = BfSymbol(source=source, symbol=symbol, display_name=symbol)
    inst = Instrument(exchange="NFO" if source == "zerodha_nfo" else "NSE", symbol=symbol, name=symbol,
                      instrument_type="option" if source == "zerodha_nfo" else "equity", data_source="zerodha_kite", external_ref=symbol)
    db.add_all([bf, inst])
    await db.flush()
    return bf, inst


def _kite(bf: BfSymbol, ts: datetime, o, h, l, c, v, oi=None) -> BfOhlcvBar:
    return BfOhlcvBar(symbol_id=bf.id, timeframe="15m", ts=ts, open=o, high=h, low=l, close=c, volume=v, open_interest=oi)


def _chart(inst: Instrument, ts: datetime, o, h, l, c, v, oi=None, source="zerodha_kite") -> OhlcvCandle:
    return OhlcvCandle(instrument_id=inst.id, timeframe="15m", ts=ts, open=o, high=h, low=l, close=c, volume=v, open_interest=oi, source=source)


async def _candle(db, inst: Instrument, ts: datetime) -> OhlcvCandle:
    """The row as stored now (populate_existing: not the test's own copy)."""
    return (
        await db.execute(
            select(OhlcvCandle).where(OhlcvCandle.instrument_id == inst.id, OhlcvCandle.ts == ts).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_a_live_oi_snapshot_is_replaced_by_kites_final_candle(db_session):
    bf, inst = await _pair(db_session, "zerodha_nfo", "NIFTY26O0622800CE")
    ts = TODAY_0930 - timedelta(days=1)
    db_session.add_all([
        _kite(bf, ts, 131.25, 133.0, 121.3, 125.0, 180000.0, oi=5_200_000.0),
        _chart(inst, ts, 128.0, 128.5, 126.0, 126.0, None, oi=5_100_000.0, source="kite_live"),  # sampled, no volume
    ])
    await db_session.commit()

    result = await catalog_sync.sync_symbol_to_catalog(db_session, bf)
    await db_session.commit()

    candle = await _candle(db_session, inst, ts)
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume, candle.open_interest) == (131.25, 133.0, 121.3, 125.0, 180000.0, 5_200_000.0)
    assert candle.source == "bf_zerodha_nfo" and result.bars_synced == 1


async def test_a_candle_saved_before_it_finished_is_replaced_and_a_matching_one_left(db_session):
    bf, inst = await _pair(db_session, "zerodha", "INFY")
    forming, matching = TODAY_0930 - timedelta(days=8), TODAY_0930 - timedelta(days=8, minutes=15)
    db_session.add_all([
        _kite(bf, forming, 1500.0, 1506.0, 1498.0, 1504.2, 90000.0),
        _chart(inst, forming, 1500.0, 1503.0, 1498.0, 1502.1, 61000.0),  # saved 10 minutes in
        _kite(bf, matching, 1497.0, 1501.0, 1496.0, 1500.0, 80000.0),
        _chart(inst, matching, 1497.0, 1501.0, 1496.0, 1500.0, 80000.0, oi=None, source="zerodha_kite"),
    ])
    await db_session.commit()

    result = await catalog_sync.sync_symbol_to_catalog(db_session, bf)
    await db_session.commit()

    fixed = await _candle(db_session, inst, forming)
    assert (fixed.high, fixed.close, fixed.volume) == (1506.0, 1504.2, 90000.0)
    kept = await _candle(db_session, inst, matching)
    assert kept.source == "zerodha_kite"  # untouched
    assert (result.bars_synced, result.bars_skipped) == (1, 1)

    again = await catalog_sync.sync_symbol_to_catalog(db_session, bf)
    assert (again.bars_synced, again.bars_skipped) == (0, 2)  # nothing left to replace


async def test_older_history_is_left_as_it_is(db_session):
    bf, inst = await _pair(db_session, "zerodha", "TCS")
    old = TODAY_0930 - timedelta(days=catalog_sync.REPLACE_DAYS + 2)
    db_session.add_all([_kite(bf, old, 4000.0, 4010.0, 3990.0, 4005.0, 5000.0), _chart(inst, old, 4000.0, 4008.0, 3990.0, 4001.0, 3000.0)])
    await db_session.commit()

    await catalog_sync.sync_symbol_to_catalog(db_session, bf)
    await db_session.commit()

    assert (await _candle(db_session, inst, old)).close == 4001.0


async def test_kites_candle_without_oi_keeps_the_oi_on_file(db_session):
    bf, inst = await _pair(db_session, "zerodha_nfo", "NIFTY26OCTFUT")
    ts = TODAY_0930 - timedelta(days=2)
    db_session.add_all([_kite(bf, ts, 23000.0, 23050.0, 22990.0, 23040.0, 7000.0), _chart(inst, ts, 23000.0, 23020.0, 22990.0, 23010.0, None, oi=9_000_000.0)])
    await db_session.commit()

    await catalog_sync.sync_symbol_to_catalog(db_session, bf)
    await db_session.commit()

    candle = await _candle(db_session, inst, ts)
    assert (candle.close, candle.open_interest) == (23040.0, 9_000_000.0)
