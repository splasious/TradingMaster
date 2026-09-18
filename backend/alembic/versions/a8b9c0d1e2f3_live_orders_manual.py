"""live_orders: nullable deployment_id + instrument/broker_account/owner columns for manual (deployment-free) orders

Revision ID: a8b9c0d1e2f3
Revises: a7b8c9d0e1f2
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a8b9c0d1e2f3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('live_orders', 'deployment_id', existing_type=sa.Uuid(), nullable=True)
    op.add_column('live_orders', sa.Column('instrument_id', sa.Uuid(), nullable=True))
    op.add_column('live_orders', sa.Column('broker_account_id', sa.Uuid(), nullable=True))
    op.add_column('live_orders', sa.Column('owner_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_live_orders_instrument_id', 'live_orders', 'instruments', ['instrument_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_live_orders_broker_account_id', 'live_orders', 'broker_accounts', ['broker_account_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_live_orders_owner_id', 'live_orders', 'users', ['owner_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_live_orders_owner_id', 'live_orders', type_='foreignkey')
    op.drop_constraint('fk_live_orders_broker_account_id', 'live_orders', type_='foreignkey')
    op.drop_constraint('fk_live_orders_instrument_id', 'live_orders', type_='foreignkey')
    op.drop_column('live_orders', 'owner_id')
    op.drop_column('live_orders', 'broker_account_id')
    op.drop_column('live_orders', 'instrument_id')
    op.alter_column('live_orders', 'deployment_id', existing_type=sa.Uuid(), nullable=False)
