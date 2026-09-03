"""add users.password_reset_requested for admin-assisted password reset

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-03 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('password_reset_requested', sa.Boolean(), nullable=False, server_default='false'))
    op.alter_column('users', 'password_reset_requested', server_default=None)


def downgrade() -> None:
    op.drop_column('users', 'password_reset_requested')
