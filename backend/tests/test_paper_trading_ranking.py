import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.paper_trading import ranking as ranking_module
from app.services.paper_trading.ranking import RANK_LOOKBACK_BARS, get_universe_ranks


async def _make_instrument(db_session: AsyncSession, symbol: str, closes: list[float]) -> Instrument:
    instrument = Instrument(
        exchange="DELTA", symbol=symbol, name=symbol, instrument_type="perpetual_future",
        data_source="delta_exchange", external_ref=symbol,
    )
    db_session.add(instrument)
    await db_session.flush()
    base = datetime.now(timezone.utc) - timedelta(days=len(closes))
    for i, close in enumerate(closes):
        db_session.add(
            OhlcvCandle(
                instrument_id=instrument.id, timeframe="1d", ts=base + timedelta(days=i),
                open=close, high=close + 1, low=close - 1, close=close, volume=1000, source="test",
            )
        )
    await db_session.commit()
    return instrument


async def test_ranks_by_trailing_momentum_strongest_first(db_session: AsyncSession):
    ranking_module._cache.clear()
    n = RANK_LOOKBACK_BARS + 1
    strong = await _make_instrument(db_session, "STRONGUSD", [100 + i * 3 for i in range(n)])  # +3/bar
    weak = await _make_instrument(db_session, "WEAKUSD", [100 - i * 1 for i in range(n)])  # -1/bar
    flat = await _make_instrument(db_session, "FLATUSD", [100.0] * n)

    version_id = uuid.uuid4()
    ranks = await get_universe_ranks(db_session, version_id, [strong.id, weak.id, flat.id], "1d", top_n=1)

    assert ranks[strong.id]["rank"] == 1
    assert ranks[flat.id]["rank"] == 2
    assert ranks[weak.id]["rank"] == 3
    assert ranks[strong.id]["in_top_n"] == 1.0
    assert ranks[weak.id]["in_top_n"] == 0.0
    assert ranks[weak.id]["in_bottom_n"] == 1.0
    assert ranks[strong.id]["total"] == 3


async def test_insufficient_history_excludes_instrument_from_ranking(db_session: AsyncSession):
    ranking_module._cache.clear()
    n = RANK_LOOKBACK_BARS + 1
    enough = await _make_instrument(db_session, "ENOUGHUSD", [100 + i for i in range(n)])
    too_few = await _make_instrument(db_session, "TOOFEWUSD", [100.0, 101.0, 102.0])  # far short of the lookback

    ranks = await get_universe_ranks(db_session, uuid.uuid4(), [enough.id, too_few.id], "1d", top_n=5)

    assert enough.id in ranks
    assert too_few.id not in ranks
    assert ranks[enough.id]["total"] == 1


async def test_result_is_cached_within_ttl(db_session: AsyncSession, monkeypatch):
    ranking_module._cache.clear()
    n = RANK_LOOKBACK_BARS + 1
    inst = await _make_instrument(db_session, "CACHEDUSD", [100 + i for i in range(n)])
    version_id = uuid.uuid4()

    first = await get_universe_ranks(db_session, version_id, [inst.id], "1d", top_n=5)

    # Add a brand-new instrument the cached result should NOT reflect --
    # proves the second call reused the cache instead of recomputing.
    other = await _make_instrument(db_session, "NEWUSD", [50 + i for i in range(n)])
    second = await get_universe_ranks(db_session, version_id, [inst.id, other.id], "1d", top_n=5)

    assert first == second
    assert other.id not in second
