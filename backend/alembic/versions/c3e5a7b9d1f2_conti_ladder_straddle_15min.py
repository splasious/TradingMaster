"""Conti Ladder Straddle 15 MIN: the strategy, its 20-lakh pool and its paper card (22 Oct).

A new built-in (native_strategies/nifty_conti_ladder.py): Conti Straddle 15
MIN's straddle spread over five strikes -- the middle one picked by the PCR
as Conti picks its strike, and 100 and 200 points either side of it, 2 lots
a leg each (10 lots a side in all) -- held from 09:45 to 15:00, shifting
when a finished 15-minute NIFTY close is 100 points from where it opened.
Created for the owner of AM OP TRD 15 MIN, as Conti Straddle was: the
strategy with version 1 the built-in's code, its own pool "Conti Ladder
Straddle 15 MIN" of Rs 20,00,000, and an active paper card on it -- no
live run, no Telegram alerts. Nothing when that owner has a strategy of
this name already. Recorded in audit_logs.

Revision ID: c3e5a7b9d1f2
Revises: b1d3f5a7c9e0
Create Date: 2026-10-22 09:00:00.000000

"""

import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5a7b9d1f2"
down_revision: Union[str, None] = "b1d3f5a7c9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = "Conti Ladder Straddle 15 MIN"
FROM_STRATEGY = "am op trd 15 min"
CAPITAL = 2_000_000.0
BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_conti_ladder.py"
DESCRIPTION = "Five NIFTY short straddles 100 pts apart, 2 lots each, 09:45-15:00; PCR picks the middle strike; shifts on a 15-min close 100 pts away (22 Oct)"

strategies = sa.table(
    "strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String), sa.column("description", sa.String),
    sa.column("owner_id", sa.Uuid), sa.column("code_type", sa.String), sa.column("status", sa.String),
)
versions = sa.table(
    "strategy_versions", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("version_number", sa.Integer),
    sa.column("timeframe", sa.String), sa.column("instrument_ids", sa.JSON), sa.column("parameters", sa.JSON),
    sa.column("python_code", sa.Text), sa.column("position_sizing", sa.JSON), sa.column("risk_rules", sa.JSON),
    sa.column("created_by", sa.Uuid),
)
portfolios = sa.table(
    "paper_portfolios", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("name", sa.String),
    sa.column("currency", sa.String), sa.column("cash", sa.Float), sa.column("initial_capital", sa.Float),
)
deployments = sa.table(
    "paper_native_deployments", sa.column("id", sa.Uuid), sa.column("portfolio_id", sa.Uuid), sa.column("strategy_id", sa.Uuid),
    sa.column("strategy_version_id", sa.Uuid), sa.column("status", sa.String), sa.column("state", sa.JSON),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def upgrade() -> None:
    bind = op.get_bind()
    owners = bind.execute(
        sa.select(strategies.c.owner_id).where(sa.func.lower(sa.func.trim(strategies.c.name)) == FROM_STRATEGY).distinct()
    ).scalars().all()
    code = BUILT_IN.read_text(encoding="utf-8")
    for owner in owners:
        exists = bind.execute(
            sa.select(strategies.c.id).where(strategies.c.owner_id == owner, sa.func.lower(sa.func.trim(strategies.c.name)) == NAME.lower())
        ).first()
        if exists is not None:
            continue
        strategy_id, version_id, pool_id, deployment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        bind.execute(strategies.insert().values(
            id=strategy_id, name=NAME, description=DESCRIPTION, owner_id=owner, code_type="native", status="paper_trading",
        ))
        bind.execute(versions.insert().values(
            id=version_id, strategy_id=strategy_id, version_number=1, timeframe="15m", instrument_ids=[], parameters={},
            python_code=code, position_sizing={}, risk_rules={}, created_by=owner,
        ))
        bind.execute(portfolios.insert().values(
            id=pool_id, user_id=owner, name=NAME, currency="INR", cash=CAPITAL, initial_capital=CAPITAL,
        ))
        bind.execute(deployments.insert().values(
            id=deployment_id, portfolio_id=pool_id, strategy_id=strategy_id, strategy_version_id=version_id,
            status="active", state=None,
        ))
        for action, object_type, object_id, value in (
            ("STRATEGY_CREATED", "strategy", strategy_id, {"name": NAME, "version_number": 1, "built_in": BUILT_IN.name}),
            ("PAPER_PORTFOLIO_CREATED", "paper_portfolio", pool_id, {"name": NAME, "currency": "INR", "initial_capital": CAPITAL}),
            ("PAPER_NATIVE_TRADING_STARTED", "strategy", strategy_id, {"portfolio_id": str(pool_id)}),
        ):
            bind.execute(audit_logs.insert().values(
                id=uuid.uuid4(), user_id=owner, action=action, object_type=object_type, object_id=str(object_id),
                previous_value=None, new_value=value,
            ))


def downgrade() -> None:
    pass  # stop or delete the card and the strategy from the app
