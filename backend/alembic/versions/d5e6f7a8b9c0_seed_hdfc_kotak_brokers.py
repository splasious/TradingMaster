"""Seed hdfc_securities / kotak_neo broker catalog rows

app/seed.py's BROKERS list is a standalone manual script (invoked via
`python -m app.seed`, never called from app startup) -- it does NOT run
automatically on deploy the way alembic migrations do, so adding a broker
there alone would leave the already-running production `brokers` table
without these two rows. This migration inserts them directly, the only
part of this feature that needs a real deploy-time data change.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-11 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'c4d5e6f7a8b9'
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
    for code, name in [("hdfc_securities", "HDFC Securities"), ("kotak_neo", "Kotak Neo")]:
        existing = conn.execute(sa.text("SELECT 1 FROM brokers WHERE code = :code"), {"code": code}).scalar()
        if existing is None:
            op.bulk_insert(brokers_table, [{"id": uuid.uuid4(), "code": code, "name": name, "is_enabled": True}])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM brokers WHERE code IN ('hdfc_securities', 'kotak_neo')"))
