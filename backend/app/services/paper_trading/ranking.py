"""Cross-instrument relative-strength ranking for paper-traded basket
strategies.

The Python sandbox is intentionally single-instrument: each deployment's
generate_signal(candles, params) call only ever sees its own instrument's
candle history (see engine.py's evaluate_deployment), with no network
access to go fetch any other instrument's data. A strategy attached to a
whole basket (StrategyVersion.instrument_ids with more than one entry)
still needs to know how its instrument stacks up against the rest of that
basket right now -- that's computed here, server-side, once per basket per
refresh cycle, and injected into `params` as plain numbers (rank,
universe_size, in_top_n, in_bottom_n) before the sandbox call, since
`parameters` is a numeric-only dict and can't carry the basket itself.

Ranked by trailing momentum (close now vs. close RANK_LOOKBACK_BARS bars
ago) *within the basket* -- relative strength against the instrument's own
peer group, not against an external benchmark symbol. That sidesteps
needing a benchmark symbol (which `parameters: dict[str, float]` has no
clean way to carry) while still being a real, standard RS interpretation:
"how is this one doing relative to the others in the set I actually care
about" is exactly what a rotation strategy needs to answer.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_data import OhlcvCandle

RANK_LOOKBACK_BARS = 20
CACHE_TTL_SECONDS = 30.0

# Keyed by (strategy_version_id, timeframe) -- every deployment sharing a
# strategy version and timeframe shares the identical ranking, so this
# cache turns "one rank computation per deployment per evaluate cycle"
# (which would refetch the whole basket's candles redundantly, once per
# instrument in it) into one computation shared by the whole basket.
_cache: dict[tuple[uuid.UUID, str], tuple[float, dict[uuid.UUID, dict]]] = {}


async def get_universe_ranks(
    db: AsyncSession, strategy_version_id: uuid.UUID, instrument_ids: list[uuid.UUID], timeframe: str, top_n: int,
) -> dict[uuid.UUID, dict]:
    cache_key = (strategy_version_id, timeframe)
    now = datetime.now(timezone.utc).timestamp()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    scores: dict[uuid.UUID, float] = {}
    for instrument_id in instrument_ids:
        result = await db.execute(
            select(OhlcvCandle.close)
            .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == timeframe)
            .order_by(OhlcvCandle.ts.desc())
            .limit(RANK_LOOKBACK_BARS + 1)
        )
        closes = [row[0] for row in result.all()]
        if len(closes) < RANK_LOOKBACK_BARS + 1 or not closes[-1]:
            continue
        latest, prior = closes[0], closes[-1]
        scores[instrument_id] = (latest - prior) / prior * 100

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    total = len(ranked)
    result_map: dict[uuid.UUID, dict] = {}
    for i, (instrument_id, score) in enumerate(ranked, start=1):
        result_map[instrument_id] = {
            "rank": i,
            "score": score,
            "total": total,
            "in_top_n": 1.0 if i <= top_n else 0.0,
            "in_bottom_n": 1.0 if i > total - top_n else 0.0,
        }

    _cache[cache_key] = (now, result_map)
    return result_map
