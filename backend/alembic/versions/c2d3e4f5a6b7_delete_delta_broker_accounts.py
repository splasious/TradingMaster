"""Delete the Delta Exchange broker accounts

Delta Exchange is hidden from the site (services/visibility.py), so its
saved broker accounts go, with their stored credentials and connection
rows. An account that any live deployment or live order still points at is
left alone, so no trading history is lost. Each deletion is written to
audit_logs -- label and environment only, never a credential. Delta's
instruments, candles and watchlists are kept, hidden.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-26 12:00:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

brokers = sa.table("brokers", sa.column("id", sa.Uuid), sa.column("code", sa.String))
accounts = sa.table(
    "broker_accounts", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("broker_id", sa.Uuid),
    sa.column("account_label", sa.String), sa.column("environment", sa.String),
)
credentials = sa.table("broker_credentials", sa.column("broker_account_id", sa.Uuid))
connections = sa.table("broker_connections", sa.column("broker_account_id", sa.Uuid))
deployments = sa.table("live_deployments", sa.column("broker_account_id", sa.Uuid))
orders = sa.table("live_orders", sa.column("broker_account_id", sa.Uuid))
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(accounts.c.id, accounts.c.user_id, accounts.c.account_label, accounts.c.environment)
        .join(brokers, brokers.c.id == accounts.c.broker_id)
        .where(
            brokers.c.code == "delta_exchange",
            ~sa.exists().where(deployments.c.broker_account_id == accounts.c.id),
            ~sa.exists().where(orders.c.broker_account_id == accounts.c.id),
        )
    ).all()
    for account_id, user_id, label, environment in rows:
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=user_id, action="BROKER_ACCOUNT_DELETED", object_type="broker_account",
            object_id=str(account_id),
            previous_value={"broker_code": "delta_exchange", "account_label": label, "environment": environment},
            new_value={"reason": "Delta Exchange hidden from the site"},
        ))
        bind.execute(credentials.delete().where(credentials.c.broker_account_id == account_id))
        bind.execute(connections.delete().where(connections.c.broker_account_id == account_id))
        bind.execute(accounts.delete().where(accounts.c.id == account_id))


def downgrade() -> None:
    pass  # the credentials are gone; reconnect the account in Settings > Brokers
