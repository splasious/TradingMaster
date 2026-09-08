"""live_deployments product_override + live_orders product (F&O NFO order routing)

Revision ID: e5f6a7b8c9d1
Revises: d4e5f6a7b8c9
Create Date: 2026-09-08 00:00:00.000002

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d1'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('live_deployments', sa.Column('product_override', sa.String(length=10), nullable=True))
    op.add_column('live_orders', sa.Column('product', sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column('live_orders', 'product')
    op.drop_column('live_deployments', 'product_override')
