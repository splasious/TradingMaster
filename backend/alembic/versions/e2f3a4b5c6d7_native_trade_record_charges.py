"""paper_native_trades: estimated charges column, wider exit_reason

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('paper_native_trades', sa.Column('charges', sa.Float(), nullable=True))
    op.alter_column('paper_native_trades', 'exit_reason', type_=sa.String(length=100), existing_type=sa.String(length=30), existing_nullable=False)


def downgrade() -> None:
    op.alter_column(
        'paper_native_trades', 'exit_reason', type_=sa.String(length=30), existing_type=sa.String(length=100), existing_nullable=False,
        postgresql_using='left(exit_reason, 30)',
    )
    op.drop_column('paper_native_trades', 'charges')
