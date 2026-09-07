"""portfolio trade side (long/short)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'portfolio_backtest_trades',
        sa.Column('side', sa.String(length=10), nullable=False, server_default='long'),
    )
    op.alter_column('portfolio_backtest_trades', 'side', server_default=None)


def downgrade() -> None:
    op.drop_column('portfolio_backtest_trades', 'side')
