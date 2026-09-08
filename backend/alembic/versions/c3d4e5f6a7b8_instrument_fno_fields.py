"""instrument F&O fields (expiry, strike, option_type, lot_size, underlying)

Revision ID: c3d4e5f6a7b8
Revises: b7c8d9e0f1a2
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instruments', sa.Column('expiry', sa.Date(), nullable=True))
    op.add_column('instruments', sa.Column('strike', sa.Float(), nullable=True))
    op.add_column('instruments', sa.Column('option_type', sa.String(length=2), nullable=True))
    op.add_column('instruments', sa.Column('lot_size', sa.Integer(), nullable=True))
    op.add_column('instruments', sa.Column('underlying_instrument_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'instruments_underlying_instrument_id_fkey', 'instruments', 'instruments',
        ['underlying_instrument_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('instruments_underlying_instrument_id_fkey', 'instruments', type_='foreignkey')
    op.drop_column('instruments', 'underlying_instrument_id')
    op.drop_column('instruments', 'lot_size')
    op.drop_column('instruments', 'option_type')
    op.drop_column('instruments', 'strike')
    op.drop_column('instruments', 'expiry')
