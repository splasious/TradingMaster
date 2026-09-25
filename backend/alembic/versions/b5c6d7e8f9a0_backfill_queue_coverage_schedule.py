"""backfill platform: durable job queue, coverage watermarks, daily top-up schedule

- bf_backfill_jobs: priority / attempts / run_after / run_id, so the
  database is the queue (one worker, survives restarts) and jobs of a
  top-up run can be counted.
- bf_backfill_runs: one row per top-up (daily automatic or "Top up now").
- bf_coverage: saved-up-to watermark per symbol and timeframe, filled in
  the background after startup (see coverage.py) -- building it here
  would scan 20M bars inside the deploy's startup.
- bf_settings: the schedule, as one row; Delta Exchange starts paused.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'a4b5c6d7e8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bf_backfill_runs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('session_date', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('jobs_total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('message', sa.String(length=300), nullable=True),
        sa.Column('requested_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'bf_coverage',
        sa.Column('symbol_id', sa.Uuid(), nullable=False),
        sa.Column('timeframe', sa.String(length=10), nullable=False),
        sa.Column('first_ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('bar_count', sa.Integer(), nullable=False),
        sa.Column('last_day_bars', sa.Integer(), nullable=True),
        sa.Column('checked_through', sa.Date(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['symbol_id'], ['bf_symbols.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('symbol_id', 'timeframe'),
    )
    settings = op.create_table(
        'bf_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('auto_topup_zerodha', sa.Boolean(), nullable=False),
        sa.Column('auto_topup_zerodha_nfo', sa.Boolean(), nullable=False),
        sa.Column('delta_enabled', sa.Boolean(), nullable=False),
        sa.Column('topup_time', sa.String(length=5), nullable=False),
        sa.Column('topup_timeframes', sa.JSON(), nullable=False),
        sa.Column('coverage_built_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.bulk_insert(settings, [{
        'id': 1, 'auto_topup_zerodha': True, 'auto_topup_zerodha_nfo': True, 'delta_enabled': False,
        'topup_time': '16:15', 'topup_timeframes': ['1m', '5m', '15m', '30m', '60m', '1d'],
    }])

    op.add_column('bf_backfill_jobs', sa.Column('priority', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('bf_backfill_jobs', sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('bf_backfill_jobs', sa.Column('run_after', sa.DateTime(timezone=True), nullable=True))
    op.add_column('bf_backfill_jobs', sa.Column('run_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_bf_backfill_jobs_run_id', 'bf_backfill_jobs', 'bf_backfill_runs', ['run_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index('ix_bf_backfill_jobs_status_priority', 'bf_backfill_jobs', ['status', 'priority', 'created_at'])
    op.create_index('ix_bf_backfill_jobs_symbol_id', 'bf_backfill_jobs', ['symbol_id'])
    op.create_index('ix_bf_backfill_jobs_run_id', 'bf_backfill_jobs', ['run_id'])


def downgrade() -> None:
    op.drop_index('ix_bf_backfill_jobs_run_id', table_name='bf_backfill_jobs')
    op.drop_index('ix_bf_backfill_jobs_symbol_id', table_name='bf_backfill_jobs')
    op.drop_index('ix_bf_backfill_jobs_status_priority', table_name='bf_backfill_jobs')
    op.drop_constraint('fk_bf_backfill_jobs_run_id', 'bf_backfill_jobs', type_='foreignkey')
    op.drop_column('bf_backfill_jobs', 'run_id')
    op.drop_column('bf_backfill_jobs', 'run_after')
    op.drop_column('bf_backfill_jobs', 'attempts')
    op.drop_column('bf_backfill_jobs', 'priority')
    op.drop_table('bf_settings')
    op.drop_table('bf_coverage')
    op.drop_table('bf_backfill_runs')
