"""OI Change Buy 15 MIN: its paper trades closed on or before 23 Nov removed (25 Nov).

Asked 25 Nov: "delete all trade record of strategy OI Change Buy 15 MIN 23
Nov and past" -- the trades of versions 1 to 5, before the 2-candle average
(version 6, 24 Nov) and the overnight hold (version 7, 25 Nov). Agreed the
same day: the trade rows only -- the pool's cash is left as it is, so it
keeps those trades' result.

Every paper trade (paper_native_trades) of a card of a strategy named OI
Change Buy 15 MIN that closed before 24 Nov 00:00 IST (CUTOFF) is deleted;
trades closed since, the open position and the card's state are left alone,
as are the live runs' trades and the audit trail of each order (audit_logs
PAPER_NATIVE_LEG_*). What was deleted -- how many, their first and last
close and their P&L in all -- goes to audit_logs, one row a card. A second
run finds nothing and writes nothing.

Revision ID: c8e0a2b4d6f7
Revises: a6c8e0b2d4f5
Create Date: 2026-11-25 12:00:00.000000

"""

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8e0a2b4d6f7"
down_revision: Union[str, None] = "a6c8e0b2d4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = "OI Change Buy 15 MIN"
CUTOFF = datetime(2026, 11, 23, 18, 30, tzinfo=timezone.utc)  # 24 Nov 00:00 IST: trades closed before it go
REASON = "OI Change Buy 15 MIN's trades closed on or before 23 Nov removed; the pool's cash left as it is (asked 25 Nov)"

strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
deployments = sa.table("paper_native_deployments", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid))
trades = sa.table(
    "paper_native_trades", sa.column("id", sa.Uuid), sa.column("deployment_id", sa.Uuid),
    sa.column("closed_at", sa.DateTime(timezone=True)), sa.column("pnl", sa.Float), sa.column("charges", sa.Float),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def upgrade() -> None:
    bind = op.get_bind()
    cards = bind.execute(
        sa.select(deployments.c.id).join(strategies, strategies.c.id == deployments.c.strategy_id)
        .where(sa.func.lower(sa.func.trim(strategies.c.name)) == NAME.lower())
    ).scalars().all()
    for card in cards:
        old = trades.c.deployment_id == card
        before = sa.and_(old, trades.c.closed_at < CUTOFF)
        count, first, last, pnl, charges = bind.execute(
            sa.select(sa.func.count(), sa.func.min(trades.c.closed_at), sa.func.max(trades.c.closed_at),
                      sa.func.coalesce(sa.func.sum(trades.c.pnl), 0.0), sa.func.coalesce(sa.func.sum(trades.c.charges), 0.0))
            .where(before)
        ).one()
        if not count:
            continue
        bind.execute(trades.delete().where(before))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="PAPER_NATIVE_TRADES_DELETED", object_type="paper_native_deployment",
            object_id=str(card),
            previous_value={"trades": int(count), "first_closed_at": str(first), "last_closed_at": str(last),
                            "pnl": round(float(pnl), 2), "charges": round(float(charges), 2)},
            new_value={"trades": 0, "closed_before": CUTOFF.isoformat(), "pool_cash_changed": False, "reason": REASON},
        ))


def downgrade() -> None:
    pass  # deleted rows aren't kept: the audit row records what went
