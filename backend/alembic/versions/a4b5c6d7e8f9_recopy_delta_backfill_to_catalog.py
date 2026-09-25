"""bf_symbols: re-copy Delta symbols' bars to the main candle table once

Minutes the Delta live sync saved never reached ohlcv_candles -- the
catalog sync only copied a symbol after a backfill job, and the live sync
didn't copy at all (it does now). Clearing last_synced_at makes
CatalogSyncScheduler copy each Delta symbol again, skipping candles
already there, which fills the gap (1.87M 1m bars on production).

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE bf_symbols SET last_synced_at = NULL WHERE source = 'delta'")


def downgrade() -> None:
    pass  # nothing to undo: the copy only adds missing candles
