"""paper_native_deployments: owner's card order on the Paper Trading page

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-09-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('paper_native_deployments', sa.Column('display_order', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('paper_native_deployments', 'display_order')
