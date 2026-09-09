"""widen bf_symbols/bf_backfill_jobs source column (10 -> 20 chars)

Revision ID: a2b3c4d5e6f7
Revises: e5f6a7b8c9d1
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'e5f6a7b8c9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('bf_symbols', 'source', type_=sa.String(length=20))
    op.alter_column('bf_backfill_jobs', 'source', type_=sa.String(length=20))


def downgrade() -> None:
    op.alter_column('bf_backfill_jobs', 'source', type_=sa.String(length=10))
    op.alter_column('bf_symbols', 'source', type_=sa.String(length=10))
