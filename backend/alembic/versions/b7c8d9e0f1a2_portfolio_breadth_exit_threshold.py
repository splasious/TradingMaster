"""portfolio backtest jobs breadth exit threshold

Revision ID: b7c8d9e0f1a2
Revises: f6a7b8c9d0e1
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'portfolio_backtest_jobs',
        sa.Column('breadth_exit_threshold', sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('portfolio_backtest_jobs', 'breadth_exit_threshold')
