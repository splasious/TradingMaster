"""fo_oi_snapshots, fo_oi_totals, fo_scan_results: the F&O opening-momentum scan's data

fo_oi_snapshots holds every stock's current-month future + CE + PE open
interest at the close (and the 09:10 backup and 09:20), last two sessions
only; fo_oi_totals the same added up per stock, kept for good; and
fo_scan_results one row per stock per 09:20/09:25 scan, kept for good.

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
Create Date: 2026-09-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'a0b1c2d3e4f5'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fo_oi_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("mark", sa.String(length=10), nullable=False),
        sa.Column("underlying_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=3), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Float(), nullable=True),
        sa.Column("oi", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["underlying_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_date", "mark", "instrument_id", name="uq_fo_oi_snapshots_session_mark_instrument"),
    )
    op.create_index("ix_fo_oi_snapshots_underlying_session", "fo_oi_snapshots", ["underlying_id", "session_date", "mark"])

    op.create_table(
        "fo_oi_totals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("mark", sa.String(length=10), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("underlying_id", sa.Uuid(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("fut_oi", sa.Float(), nullable=True),
        sa.Column("ce_oi", sa.Float(), nullable=True),
        sa.Column("pe_oi", sa.Float(), nullable=True),
        sa.Column("total_oi", sa.Float(), nullable=True),
        sa.Column("contracts_listed", sa.Integer(), nullable=False),
        sa.Column("contracts_with_oi", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["underlying_id"], ["instruments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_date", "mark", "symbol", "expiry", name="uq_fo_oi_totals_session_mark_symbol_expiry"),
    )
    op.create_index("ix_fo_oi_totals_session_date", "fo_oi_totals", ["session_date"])

    op.create_table(
        "fo_scan_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=True),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("scan", sa.String(length=5), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("underlying_id", sa.Uuid(), nullable=True),
        sa.Column("direction", sa.String(length=2), nullable=True),
        sa.Column("prev_close", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("move_pct", sa.Float(), nullable=True),
        sa.Column("oi_baseline", sa.String(length=20), nullable=True),
        sa.Column("oi_prev_total", sa.Float(), nullable=True),
        sa.Column("oi_now_total", sa.Float(), nullable=True),
        sa.Column("oi_change_pct", sa.Float(), nullable=True),
        sa.Column("fut_prev", sa.Float(), nullable=True),
        sa.Column("fut_now", sa.Float(), nullable=True),
        sa.Column("ce_prev", sa.Float(), nullable=True),
        sa.Column("ce_now", sa.Float(), nullable=True),
        sa.Column("pe_prev", sa.Float(), nullable=True),
        sa.Column("pe_now", sa.Float(), nullable=True),
        sa.Column("legs_counted", sa.Integer(), nullable=True),
        sa.Column("legs_listed", sa.Integer(), nullable=True),
        sa.Column("retrace_pct", sa.Float(), nullable=True),
        sa.Column("passed_move", sa.Boolean(), nullable=False),
        sa.Column("passed_oi", sa.Boolean(), nullable=True),
        sa.Column("passed_retrace", sa.Boolean(), nullable=True),
        sa.Column("nifty_bias", sa.String(length=12), nullable=True),
        sa.Column("passed_nifty", sa.Boolean(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("breakout_high", sa.Float(), nullable=True),
        sa.Column("breakout_low", sa.Float(), nullable=True),
        sa.Column("option_symbol", sa.String(length=50), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_premium", sa.Float(), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_premium", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(length=100), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["deployment_id"], ["paper_native_deployments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["underlying_id"], ["instruments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "session_date", "scan", "symbol", name="uq_fo_scan_results_deployment_session_scan_symbol"),
    )
    op.create_index("ix_fo_scan_results_session", "fo_scan_results", ["session_date", "scan"])


def downgrade() -> None:
    op.drop_index("ix_fo_scan_results_session", table_name="fo_scan_results")
    op.drop_table("fo_scan_results")
    op.drop_index("ix_fo_oi_totals_session_date", table_name="fo_oi_totals")
    op.drop_table("fo_oi_totals")
    op.drop_index("ix_fo_oi_snapshots_underlying_session", table_name="fo_oi_snapshots")
    op.drop_table("fo_oi_snapshots")
