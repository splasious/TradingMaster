"""portfolio backtest

Revision ID: c8d1e3f5a9b2
Revises: b2c3d4e5f6a7
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d1e3f5a9b2'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('portfolio_backtest_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('strategy_id', sa.Uuid(), nullable=False),
    sa.Column('strategy_version_id', sa.Uuid(), nullable=False),
    sa.Column('instrument_ids', sa.JSON(), nullable=False),
    sa.Column('timeframe', sa.String(length=10), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=True),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('initial_capital', sa.Float(), nullable=False),
    sa.Column('position_size_pct', sa.Float(), nullable=False),
    sa.Column('max_open_positions', sa.Integer(), nullable=False),
    sa.Column('brokerage_pct', sa.Float(), nullable=False),
    sa.Column('slippage_pct', sa.Float(), nullable=False),
    sa.Column('tax_pct', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error_message', sa.String(length=1000), nullable=True),
    sa.Column('requested_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['strategy_versions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('portfolio_backtest_results',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('metrics', sa.JSON(), nullable=False),
    sa.Column('equity_curve', sa.JSON(), nullable=False),
    sa.Column('instrument_count', sa.Integer(), nullable=False),
    sa.Column('skipped_symbols', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['portfolio_backtest_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id')
    )
    op.create_table('portfolio_backtest_trades',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('instrument_id', sa.Uuid(), nullable=False),
    sa.Column('symbol', sa.String(length=50), nullable=False),
    sa.Column('entry_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('entry_price', sa.Float(), nullable=False),
    sa.Column('exit_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('exit_price', sa.Float(), nullable=True),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('pnl', sa.Float(), nullable=False),
    sa.Column('pnl_pct', sa.Float(), nullable=False),
    sa.Column('bars_held', sa.Integer(), nullable=False),
    sa.Column('exit_reason', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['instrument_id'], ['instruments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['job_id'], ['portfolio_backtest_jobs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('portfolio_backtest_trades')
    op.drop_table('portfolio_backtest_results')
    op.drop_table('portfolio_backtest_jobs')
