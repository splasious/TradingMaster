"""pcr_snapshots, pcr_snapshot_expiries, pcr_strike_oi: 15-minute NIFTY PCR records

One record every 15 minutes, 09:00-15:30 IST: OI totals and PCR over ATM
±40 strikes of the next 4 weekly expiries, what changed since the previous
record, the same per expiry, and every captured contract's OI (±60).

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-09-25 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pcr_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("underlying", sa.String(length=20), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("strike_window", sa.Integer(), nullable=False),
        sa.Column("expiries", sa.JSON(), nullable=False),
        sa.Column("spot", sa.Float(), nullable=True),
        sa.Column("atm_strike", sa.Float(), nullable=True),
        sa.Column("strike_step", sa.Float(), nullable=True),
        sa.Column("contracts_expected", sa.Integer(), nullable=False),
        sa.Column("contracts_with_oi", sa.Integer(), nullable=False),
        sa.Column("total_call_oi", sa.Float(), nullable=True),
        sa.Column("total_put_oi", sa.Float(), nullable=True),
        sa.Column("pcr", sa.Float(), nullable=True),
        sa.Column("prev_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prev_pcr", sa.Float(), nullable=True),
        sa.Column("pcr_change", sa.Float(), nullable=True),
        sa.Column("spot_change", sa.Float(), nullable=True),
        sa.Column("spot_change_pct", sa.Float(), nullable=True),
        sa.Column("atm_shift", sa.Float(), nullable=True),
        sa.Column("call_oi_change", sa.Float(), nullable=True),
        sa.Column("put_oi_change", sa.Float(), nullable=True),
        sa.Column("oi_change_pcr", sa.Float(), nullable=True),
        sa.Column("oi_change_contracts", sa.Integer(), nullable=True),
        sa.Column("day_baseline_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("call_oi_change_day", sa.Float(), nullable=True),
        sa.Column("put_oi_change_day", sa.Float(), nullable=True),
        sa.Column("oi_change_pcr_day", sa.Float(), nullable=True),
        sa.Column("positioning", sa.String(length=20), nullable=True),
        sa.Column("oi_driver", sa.String(length=40), nullable=True),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("calc_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("underlying", "ts", name="uq_pcr_snapshots_underlying_ts"),
    )
    op.create_index("ix_pcr_snapshots_underlying_session", "pcr_snapshots", ["underlying", "session_date"])

    op.create_table(
        "pcr_snapshot_expiries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("atm_strike", sa.Float(), nullable=True),
        sa.Column("strike_lo", sa.Float(), nullable=True),
        sa.Column("strike_hi", sa.Float(), nullable=True),
        sa.Column("contracts_expected", sa.Integer(), nullable=False),
        sa.Column("contracts_with_oi", sa.Integer(), nullable=False),
        sa.Column("total_call_oi", sa.Float(), nullable=True),
        sa.Column("total_put_oi", sa.Float(), nullable=True),
        sa.Column("pcr", sa.Float(), nullable=True),
        sa.Column("call_oi_change", sa.Float(), nullable=True),
        sa.Column("put_oi_change", sa.Float(), nullable=True),
        sa.Column("oi_change_pcr", sa.Float(), nullable=True),
        sa.Column("call_oi_change_day", sa.Float(), nullable=True),
        sa.Column("put_oi_change_day", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["snapshot_id"], ["pcr_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "expiry", name="uq_pcr_snapshot_expiries_snapshot_expiry"),
    )

    op.create_table(
        "pcr_strike_oi",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("option_type", sa.String(length=2), nullable=False),
        sa.Column("tradingsymbol", sa.String(length=50), nullable=False),
        sa.Column("oi", sa.Float(), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["snapshot_id"], ["pcr_snapshots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "expiry", "strike", "option_type", name="uq_pcr_strike_oi_contract"),
    )


def downgrade() -> None:
    op.drop_table("pcr_strike_oi")
    op.drop_table("pcr_snapshot_expiries")
    op.drop_index("ix_pcr_snapshots_underlying_session", table_name="pcr_snapshots")
    op.drop_table("pcr_snapshots")
