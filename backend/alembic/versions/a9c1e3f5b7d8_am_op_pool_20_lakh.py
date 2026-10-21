"""AM OP TRD 15 MIN's paper pool: 5 lakh -> 20 lakh starting capital (21 Oct).

10 lots per leg needs about 20 lakh of margin, so the pool's starting
capital becomes 20 lakh -- with the 15 lakh difference added to its cash,
so what the card has made stays in it (equity = 20 lakh + its P&L). Its
trades, P&L and charges are untouched; ROI, CAGR and XIRR on the card read
the pool's starting capital, so they now show the whole history on 20 lakh.

Not the pool's Edit, which resets cash to the new amount and would drop
the P&L. Only a pool named AM OP TRD 15 MIN, running that strategy, still
at 5 lakh -- once moved it's at 20 lakh, so this never applies twice.
Recorded in audit_logs.

Revision ID: a9c1e3f5b7d8
Revises: f7b9d1e3a5c6
Create Date: 2026-10-21 07:50:00.000000

"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e3f5b7d8"
down_revision: Union[str, None] = "f7b9d1e3a5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

POOL_AND_STRATEGY = "am op trd 15 min"
FROM_CAPITAL = 500_000.0
TO_CAPITAL = 2_000_000.0
REASON = "10 lots per leg needs about 20 lakh of margin (21 Oct); the P&L is kept"

portfolios = sa.table(
    "paper_portfolios", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("name", sa.String),
    sa.column("cash", sa.Float), sa.column("initial_capital", sa.Float),
)
deployments = sa.table("paper_native_deployments", sa.column("portfolio_id", sa.Uuid), sa.column("strategy_id", sa.Uuid))
strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def upgrade() -> None:
    bind = op.get_bind()
    pools = bind.execute(
        sa.select(portfolios.c.id, portfolios.c.user_id, portfolios.c.cash, portfolios.c.initial_capital)
        .where(sa.func.lower(sa.func.trim(portfolios.c.name)) == POOL_AND_STRATEGY)
        .where(sa.func.abs(portfolios.c.initial_capital - FROM_CAPITAL) < 0.01)
        .where(portfolios.c.id.in_(
            sa.select(deployments.c.portfolio_id).join(strategies, strategies.c.id == deployments.c.strategy_id)
            .where(sa.func.lower(sa.func.trim(strategies.c.name)) == POOL_AND_STRATEGY)
        ))
    ).mappings().all()
    for pool in pools:
        cash = pool["cash"] + (TO_CAPITAL - pool["initial_capital"])
        bind.execute(portfolios.update().where(portfolios.c.id == pool["id"]).values(initial_capital=TO_CAPITAL, cash=cash))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=pool["user_id"], action="PAPER_PORTFOLIO_CAPITAL_RAISED", object_type="paper_portfolio",
            object_id=str(pool["id"]), previous_value={"initial_capital": pool["initial_capital"], "cash": pool["cash"]},
            new_value={"initial_capital": TO_CAPITAL, "cash": cash, "reason": REASON},
        ))


def downgrade() -> None:
    pass  # the audit log has the old values
