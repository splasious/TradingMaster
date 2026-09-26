"""Delete stock options' saved candles in the background

bf_settings.purge_stock_options: set here, so purge.py deletes every
stock-option candle from both candle tables and bf_coverage after startup
(too many rows to delete inside the migration), then clears it. Stock
options aren't downloaded any more; index options, futures and the
contract list are kept.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a0b1c2d3e4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bf_settings", sa.Column("purge_stock_options", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE bf_settings SET purge_stock_options = true")


def downgrade() -> None:
    op.drop_column("bf_settings", "purge_stock_options")
