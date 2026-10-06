"""Queue the 4 stocks' downloads again, as the user whose Zerodha login fetches them

9b3d5f7a2c46 queued CUPID, MTARTECH, STLTECH and TDPOWERSYS's downloads
without a requesting user, and the worker logs in to Kite as the job's user
(jobs.py, _kite_broker): with none it found "No Zerodha Kite account
connected", a permanent error, so all 12 failed at once on 6 Oct at 15:41
with nothing downloaded. Each of those failed jobs goes back in the queue as
the user of the connected Zerodha account -- the one the daily top-up uses
(find_connected_zerodha_account) -- or, if none is connected right now, the
user of the latest completed Zerodha download. Running again changes
nothing; with neither user found nothing changes.

Revision ID: d6e8f0a2b4c5
Revises: c5d7e9f1a3b4
Create Date: 2026-10-06 16:00:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6e8f0a2b4c5"
down_revision: Union[str, None] = "c5d7e9f1a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SYMBOLS = ("CUPID", "MTARTECH", "STLTECH", "TDPOWERSYS")

brokers = sa.table("brokers", sa.column("id", sa.Uuid), sa.column("code", sa.String))
accounts = sa.table(
    "broker_accounts", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("broker_id", sa.Uuid),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
connections = sa.table("broker_connections", sa.column("broker_account_id", sa.Uuid), sa.column("status", sa.String))
bf_symbols = sa.table("bf_symbols", sa.column("id", sa.Uuid), sa.column("source", sa.String), sa.column("symbol", sa.String))
bf_jobs = sa.table(
    "bf_backfill_jobs", sa.column("id", sa.Uuid), sa.column("symbol_id", sa.Uuid), sa.column("source", sa.String),
    sa.column("timeframe", sa.String), sa.column("status", sa.String), sa.column("requested_by", sa.Uuid),
    sa.column("attempts", sa.Integer), sa.column("error_message", sa.String), sa.column("started_at", sa.DateTime(timezone=True)),
    sa.column("completed_at", sa.DateTime(timezone=True)), sa.column("run_after", sa.DateTime(timezone=True)),
    sa.column("downloaded_count", sa.Integer), sa.column("inserted_count", sa.Integer), sa.column("duplicate_count", sa.Integer),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def _kite_user(bind) -> uuid.UUID | None:
    connected = bind.execute(
        sa.select(accounts.c.user_id)
        .join(brokers, brokers.c.id == accounts.c.broker_id)
        .join(connections, connections.c.broker_account_id == accounts.c.id)
        .where(brokers.c.code == "zerodha_kite", connections.c.status == "connected")
        .order_by(accounts.c.created_at.desc()).limit(1)
    ).scalar_one_or_none()
    if connected is not None:
        return connected
    return bind.execute(
        sa.select(bf_jobs.c.requested_by)
        .where(bf_jobs.c.source == "zerodha", bf_jobs.c.status == "completed", bf_jobs.c.requested_by.is_not(None))
        .order_by(bf_jobs.c.completed_at.desc()).limit(1)
    ).scalar_one_or_none()


def upgrade() -> None:
    bind = op.get_bind()
    failed = bind.execute(
        sa.select(bf_jobs.c.id, bf_symbols.c.symbol, bf_jobs.c.timeframe)
        .join(bf_symbols, bf_symbols.c.id == bf_jobs.c.symbol_id)
        .where(bf_symbols.c.source == "zerodha", bf_symbols.c.symbol.in_(SYMBOLS), bf_jobs.c.source == "zerodha",
               bf_jobs.c.status == "failed", bf_jobs.c.requested_by.is_(None))
    ).all()
    if not failed:
        return
    user_id = _kite_user(bind)
    if user_id is None:
        return  # no Zerodha login to fetch with: left failed, retried from the Data Backfill page
    bind.execute(
        bf_jobs.update().where(bf_jobs.c.id.in_([job_id for job_id, _, _ in failed])).values(
            status="pending", requested_by=user_id, attempts=0, error_message=None, started_at=None, completed_at=None,
            run_after=None, downloaded_count=0, inserted_count=0, duplicate_count=0,
        )
    )
    bind.execute(audit_logs.insert().values(
        id=uuid.uuid4(), user_id=None, action="BF_BACKFILL_STARTED", object_type="bf_backfill_job", object_id=None,
        previous_value={"status": "failed", "jobs": len(failed)},
        new_value={"status": "pending", "symbols": sorted({symbol for _, symbol, _ in failed}),
                   "reason": "queued again as the Zerodha user: the 6 Oct 15:41 jobs had none and failed at once"},
    ))


def downgrade() -> None:
    pass  # the downloaded history stays
