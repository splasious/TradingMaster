"""open_interest columns (ohlcv_candles, bf_ohlcv_bars)

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, None] = 'a2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ohlcv_candles', sa.Column('open_interest', sa.Float(), nullable=True))
    op.add_column('bf_ohlcv_bars', sa.Column('open_interest', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('bf_ohlcv_bars', 'open_interest')
    op.drop_column('ohlcv_candles', 'open_interest')
