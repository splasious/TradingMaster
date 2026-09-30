"""Copy once more the NFO contracts whose live-feed candles differ from Kite's

The evening copy replaces a chart candle of the last 21 days that differs
from Kite's final one (catalog_sync.py), but only for a contract it copies
again -- one downloaded since its last copy. About 160 NIFTY contracts of
the weekly that expired on 29 Sep were last copied that evening, hours
before that repair went live, and being expired they're never downloaded
again: ~20,000 of their live-feed ("kite_live") 15-minute candles of 22-28
Sep stayed as sampled (health check R3e). This marks every such contract as
not yet copied, so the copy scheduler picks it up and replaces them.

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-09-30 21:00:00.000000

"""
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REPLACE_DAYS = 21  # catalog_sync.REPLACE_DAYS

bf_symbols = sa.table("bf_symbols", sa.column("id", sa.Uuid), sa.column("source", sa.String), sa.column("symbol", sa.String),
                      sa.column("last_synced_at", sa.DateTime(timezone=True)))
bf_bars = sa.table("bf_ohlcv_bars", sa.column("symbol_id", sa.Uuid), sa.column("timeframe", sa.String),
                   sa.column("ts", sa.DateTime(timezone=True)), sa.column("close", sa.Float), sa.column("volume", sa.Float))
instruments = sa.table("instruments", sa.column("id", sa.Uuid), sa.column("exchange", sa.String), sa.column("symbol", sa.String))
candles = sa.table("ohlcv_candles", sa.column("instrument_id", sa.Uuid), sa.column("timeframe", sa.String),
                   sa.column("ts", sa.DateTime(timezone=True)), sa.column("close", sa.Float), sa.column("volume", sa.Float),
                   sa.column("source", sa.String))


def upgrade() -> None:
    since = datetime.now(timezone.utc) - timedelta(days=REPLACE_DAYS)
    differs = (
        sa.select(bf_symbols.c.id)
        .join(instruments, sa.and_(instruments.c.exchange == "NFO", instruments.c.symbol == bf_symbols.c.symbol))
        .join(candles, candles.c.instrument_id == instruments.c.id)
        .join(bf_bars, sa.and_(bf_bars.c.symbol_id == bf_symbols.c.id, bf_bars.c.timeframe == candles.c.timeframe, bf_bars.c.ts == candles.c.ts))
        .where(
            bf_symbols.c.source == "zerodha_nfo", candles.c.source == "kite_live", candles.c.ts >= since,
            sa.or_(candles.c.close != bf_bars.c.close, candles.c.volume.is_distinct_from(bf_bars.c.volume)),
        )
        .distinct()
    )
    op.get_bind().execute(bf_symbols.update().where(bf_symbols.c.id.in_(differs)).values(last_synced_at=None))


def downgrade() -> None:
    pass  # the next copy sets last_synced_at again
