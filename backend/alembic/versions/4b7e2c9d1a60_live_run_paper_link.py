"""A live run remembers the paper run it was switched on from (the Trading
page's Paper / Live switch)

Additive only -- one nullable live_native_deployments column with its index.

Revision ID: 4b7e2c9d1a60
Revises: 2dc08a24c052
Create Date: 2026-10-02 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4b7e2c9d1a60"
down_revision: Union[str, None] = "2dc08a24c052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "live_native_deployments",
        sa.Column("paper_deployment_id", sa.Uuid(), sa.ForeignKey("paper_native_deployments.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_live_native_deployments_paper_deployment_id", "live_native_deployments", ["paper_deployment_id"])


def downgrade() -> None:
    op.drop_index("ix_live_native_deployments_paper_deployment_id", table_name="live_native_deployments")
    op.drop_column("live_native_deployments", "paper_deployment_id")
