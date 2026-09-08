"""bf_symbols F&O metadata (expiry, strike, option_type, lot_size, underlying_symbol)

Revision ID: f1a2b3c4d5e6
Revises: c3d4e5f6a7b8
Create Date: 2026-09-08 00:00:00.000001

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bf_symbols', sa.Column('expiry', sa.Date(), nullable=True))
    op.add_column('bf_symbols', sa.Column('strike', sa.Float(), nullable=True))
    op.add_column('bf_symbols', sa.Column('option_type', sa.String(length=2), nullable=True))
    op.add_column('bf_symbols', sa.Column('lot_size', sa.Integer(), nullable=True))
    op.add_column('bf_symbols', sa.Column('underlying_symbol', sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column('bf_symbols', 'underlying_symbol')
    op.drop_column('bf_symbols', 'lot_size')
    op.drop_column('bf_symbols', 'option_type')
    op.drop_column('bf_symbols', 'strike')
    op.drop_column('bf_symbols', 'expiry')
