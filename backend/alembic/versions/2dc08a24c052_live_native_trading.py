"""Native strategies trading live: deployments, their real holdings, trades,
the account-wide loss limit, and fills on live orders

Additive only -- four new tables and five nullable live_orders columns
(models/live_native.py, models/live_trading.py). Nothing existing changes.

Revision ID: 2dc08a24c052
Revises: 7dab5ef6e158
Create Date: 2026-10-01 10:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2dc08a24c052"
down_revision: Union[str, None] = "7dab5ef6e158"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "live_native_deployments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.Uuid(), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(), sa.ForeignKey("strategy_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), sa.ForeignKey("broker_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("state", sa.JSON(), nullable=True),
        sa.Column("lots_per_leg", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("capital", sa.Float(), nullable=False),
        sa.Column("daily_loss_limit", sa.Float(), nullable=True),
        sa.Column("max_orders_per_day", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("product_style", sa.String(20), nullable=False, server_default="overnight"),
        sa.Column("pause_reason", sa.String(500), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_signal", sa.String(20), nullable=True),
        sa.Column("last_signal_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_live_native_deployments_status", "live_native_deployments", ["status"])
    op.create_table(
        "live_native_positions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("deployment_id", sa.Uuid(), sa.ForeignKey("live_native_deployments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_id", sa.Uuid(), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("avg_price", sa.Float(), nullable=False),
        sa.Column("strategy_quantity", sa.Float(), nullable=False),
        sa.Column("product", sa.String(10), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("deployment_id", "instrument_id", name="uq_live_native_position"),
    )
    op.create_table(
        "live_native_trades",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("deployment_id", sa.Uuid(), sa.ForeignKey("live_native_deployments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("legs", sa.JSON(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=False),
        sa.Column("pnl_pct", sa.Float(), nullable=False),
        sa.Column("charges", sa.Float(), nullable=True),
        sa.Column("exit_reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_live_native_trades_deployment_closed", "live_native_trades", ["deployment_id", "closed_at"])
    op.create_table(
        "live_risk_settings",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("account_daily_loss_limit", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    with op.batch_alter_table("live_orders") as batch:
        batch.add_column(sa.Column("native_deployment_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("purpose", sa.String(20), nullable=True))
        batch.add_column(sa.Column("filled_quantity", sa.Float(), nullable=True))
        batch.add_column(sa.Column("average_price", sa.Float(), nullable=True))
        batch.add_column(sa.Column("limit_price", sa.Float(), nullable=True))
        batch.create_foreign_key(
            "fk_live_orders_native_deployment", "live_native_deployments", ["native_deployment_id"], ["id"], ondelete="CASCADE",
        )
    op.create_index("ix_live_orders_native_deployment", "live_orders", ["native_deployment_id"])


def downgrade() -> None:
    op.drop_index("ix_live_orders_native_deployment", table_name="live_orders")
    with op.batch_alter_table("live_orders") as batch:
        batch.drop_constraint("fk_live_orders_native_deployment", type_="foreignkey")
        batch.drop_column("limit_price")
        batch.drop_column("average_price")
        batch.drop_column("filled_quantity")
        batch.drop_column("purpose")
        batch.drop_column("native_deployment_id")
    op.drop_table("live_risk_settings")
    op.drop_index("ix_live_native_trades_deployment_closed", table_name="live_native_trades")
    op.drop_table("live_native_trades")
    op.drop_table("live_native_positions")
    op.drop_index("ix_live_native_deployments_status", table_name="live_native_deployments")
    op.drop_table("live_native_deployments")
