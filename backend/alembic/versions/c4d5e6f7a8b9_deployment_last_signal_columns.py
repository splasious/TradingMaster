"""last_signal / last_signal_reason columns (paper_deployments, live_deployments)

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('paper_deployments', sa.Column('last_signal', sa.String(length=20), nullable=True))
    op.add_column('paper_deployments', sa.Column('last_signal_reason', sa.String(length=500), nullable=True))
    op.add_column('live_deployments', sa.Column('last_signal', sa.String(length=20), nullable=True))
    op.add_column('live_deployments', sa.Column('last_signal_reason', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('live_deployments', 'last_signal_reason')
    op.drop_column('live_deployments', 'last_signal')
    op.drop_column('paper_deployments', 'last_signal_reason')
    op.drop_column('paper_deployments', 'last_signal')
