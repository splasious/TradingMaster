"""Seed angel_one / dhan broker catalog rows

Same reason as d5e6f7a8b9c0: app/seed.py isn't run on deploy, so the
production `brokers` table only gets new brokers through a migration.
Both are connect-and-verify only for now (registry._CONNECT_ONLY_BROKERS).

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-09-25 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c6d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

brokers_table = sa.table(
    'brokers',
    sa.column('id', sa.Uuid()),
    sa.column('code', sa.String()),
    sa.column('name', sa.String()),
    sa.column('is_enabled', sa.Boolean()),
)


def upgrade() -> None:
    conn = op.get_bind()
    for code, name in [("angel_one", "Angel One"), ("dhan", "Dhan")]:
        existing = conn.execute(sa.text("SELECT 1 FROM brokers WHERE code = :code"), {"code": code}).scalar()
        if existing is None:
            op.bulk_insert(brokers_table, [{"id": uuid.uuid4(), "code": code, "name": name, "is_enabled": True}])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM brokers WHERE code IN ('angel_one', 'dhan')"))
