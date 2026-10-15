"""telegram_chats: the Telegram groups the app's bot has been seen in

For the OI-levels and F&O scanner alerts going to a group as well
(services/notifications/telegram_group.py, 15 Oct): a bot learns a group's
ID only from its own updates, so the groups seen are kept here.

Revision ID: b3d5f7a9c1e2
Revises: a2c4e6f8b0d1
Create Date: 2026-10-15 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3d5f7a9c1e2"
down_revision: Union[str, None] = "a2c4e6f8b0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_chats",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("bot_status", sa.String(length=20), nullable=True),
        sa.Column("migrated_to", sa.BigInteger(), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("telegram_chats")
