"""bf_backfill_jobs: queued NFO top-up jobs go after every NSE one

Top-up priorities now put each NSE stock timeframe ahead of any NFO
contract (topup_priority). Jobs already queued by the running top-up keep
the priority they were given; this moves the NFO ones behind the NSE ones.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-09-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# len(TOPUP_TIMEFRAME_ORDER): the NFO offset in topup_priority
_NFO_OFFSET = 6


def upgrade() -> None:
    op.execute(
        f"UPDATE bf_backfill_jobs SET priority = priority + {_NFO_OFFSET} "
        "WHERE status = 'pending' AND source = 'zerodha_nfo' AND run_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE bf_backfill_jobs SET priority = priority - {_NFO_OFFSET} "
        "WHERE status = 'pending' AND source = 'zerodha_nfo' AND run_id IS NOT NULL"
    )
