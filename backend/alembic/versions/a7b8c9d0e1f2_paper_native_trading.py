"""paper_native_deployments / paper_native_trades (Advanced Python strategies)

Revision ID: a7b8c9d0e1f2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'paper_native_deployments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('portfolio_id', sa.Uuid(), nullable=False),
        sa.Column('strategy_id', sa.Uuid(), nullable=False),
        sa.Column('strategy_version_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('last_evaluated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_signal', sa.String(length=20), nullable=True),
        sa.Column('last_signal_reason', sa.String(length=500), nullable=True),
        sa.Column('state', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('stopped_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['portfolio_id'], ['paper_portfolios.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'paper_native_trades',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('deployment_id', sa.Uuid(), nullable=False),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('legs', sa.JSON(), nullable=False),
        sa.Column('pnl', sa.Float(), nullable=False),
        sa.Column('pnl_pct', sa.Float(), nullable=False),
        sa.Column('exit_reason', sa.String(length=30), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['deployment_id'], ['paper_native_deployments.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('paper_native_trades')
    op.drop_table('paper_native_deployments')
