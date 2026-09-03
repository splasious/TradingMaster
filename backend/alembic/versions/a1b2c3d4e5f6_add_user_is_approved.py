"""add users.is_approved for self-service signup approval

Revision ID: a1b2c3d4e5f6
Revises: 0569b14a23fd
Create Date: 2026-09-03 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '0569b14a23fd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('is_approved', sa.Boolean(), nullable=False, server_default='true'))
    op.alter_column('users', 'is_approved', server_default=None)


def downgrade() -> None:
    op.drop_column('users', 'is_approved')
