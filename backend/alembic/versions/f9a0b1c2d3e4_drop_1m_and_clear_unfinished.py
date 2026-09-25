"""Drop the 1-minute timeframe; clear unfinished and failed backfill jobs

Asked for 26 Sep 2026: 1-minute isn't used and was the largest share of
the stored candles. It leaves the daily top-up, and its saved candles are
queued for deletion (bf_settings.purge_timeframes -- deleted in the
background by services/backfill_platform/purge.py, too many rows to delete
here without holding up startup). Every failed job is dismissed (kept in
the history as cancelled, "Dismissed: ..."), anything still queued or
running is cancelled, and a run still open is closed as completed -- so
the Data Backfill page shows nothing unfinished.

The data changes are not undone by the downgrade (only the column goes).

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-09-26 00:00:00.000000

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT = ["5m", "15m", "30m", "60m", "1d"]


def upgrade() -> None:
    op.add_column("bf_settings", sa.Column("purge_timeframes", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    bind = op.get_bind()
    for row_id, timeframes in bind.execute(sa.text("SELECT id, topup_timeframes FROM bf_settings")).all():
        if isinstance(timeframes, str):
            timeframes = json.loads(timeframes)
        kept = [tf for tf in (timeframes or []) if tf != "1m"] or list(_DEFAULT)
        bind.execute(
            sa.text("UPDATE bf_settings SET topup_timeframes = CAST(:kept AS json), purge_timeframes = CAST(:purge AS json) WHERE id = :id"),
            {"kept": json.dumps(kept), "purge": json.dumps(["1m"]), "id": row_id},
        )
    op.execute(
        "UPDATE bf_backfill_jobs SET status = 'cancelled', completed_at = coalesce(completed_at, now()), "
        "error_message = left('Dismissed: ' || coalesce(error_message, ''), 1000) WHERE status = 'failed'"
    )
    op.execute(
        "UPDATE bf_backfill_jobs SET status = 'cancelled', completed_at = now(), "
        "error_message = 'Cancelled: unfinished backfill cleared' WHERE status IN ('pending', 'running')"
    )
    op.execute(
        "UPDATE bf_backfill_runs SET status = 'completed', completed_at = coalesce(completed_at, now()) "
        "WHERE status IN ('running', 'waiting_login')"
    )


def downgrade() -> None:
    op.drop_column("bf_settings", "purge_timeframes")
