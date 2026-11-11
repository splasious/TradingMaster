"""paper_native_trades.extremes: the most profit and loss a position reached (11 Nov)

A nullable JSON column for the most profit and the most loss "if closed
now" a position reached while open, with when -- saved by a strategy that
tracks them (Conti Calendar 400 15 MIN). Existing rows stay None.

Revision ID: f4b6d8a0c2e7
Revises: e2a4c6e8f0b3
Create Date: 2026-11-11 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4b6d8a0c2e7"
down_revision: Union[str, None] = "e2a4c6e8f0b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_native_trades", sa.Column("extremes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_native_trades", "extremes")
