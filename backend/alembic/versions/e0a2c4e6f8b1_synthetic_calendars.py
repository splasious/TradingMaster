"""Synthetic Calendar 15 MIN and Synthetic Calendar Reverse 15 MIN: the strategies, their pools and paper cards (20 Nov).

Two new built-ins, agreed 20 Nov from a position the user built: at the
50-point strike nearest NIFTY, 20 lots a leg at bid / ask --
native_strategies/nifty_synthetic_calendar.py buys the weekly CE and sells
the weekly PE (a synthetic long) and buys the monthly PE and sells the
monthly CE (a synthetic short); nifty_synthetic_calendar_reverse.py every leg
the other way. Opened 09:30-15:00 with a minute's pause after a close;
Rs 14,000 take-profit after charges; all four closed at 15:00 on the
weekly's expiry day; no stop-loss. Created for the owner of Conti Calendar
15 MIN: each strategy with version 1 its built-in's code, its own pool of
Rs 20,00,000 and an active paper card -- no live run, no Telegram alerts.
Nothing for a name that owner has a strategy of already. Recorded in
audit_logs.

Revision ID: e0a2c4e6f8b1
Revises: c6e8a0b2d4f1
Create Date: 2026-11-20 14:30:00.000000

"""

import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e0a2c4e6f8b1"
down_revision: Union[str, None] = "c6e8a0b2d4f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FROM_STRATEGY = "conti calendar 15 min"
CAPITAL = 2_000_000.0
NATIVE = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies"
BUILT_INS = (  # name, file, description
    ("Synthetic Calendar 15 MIN", NATIVE / "nifty_synthetic_calendar.py",
     ("NIFTY at the ATM strike, 20 lots: weekly CE bought / PE sold, monthly PE bought / CE sold; Rs 14,000 take-profit "
      "after charges; closed 15:00 on the weekly's expiry day (20 Nov)")),
    ("Synthetic Calendar Reverse 15 MIN", NATIVE / "nifty_synthetic_calendar_reverse.py",
     ("NIFTY at the ATM strike, 20 lots: weekly CE sold / PE bought, monthly PE sold / CE bought; Rs 14,000 take-profit "
      "after charges; closed 15:00 on the weekly's expiry day (20 Nov)")),
)

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
    for NAME, BUILT_IN, DESCRIPTION in BUILT_INS:
        if not BUILT_IN.is_file():
            continue
        code = BUILT_IN.read_text(encoding="utf-8")
        for owner in owners:
            exists = bind.execute(
                sa.select(strategies.c.id).where(strategies.c.owner_id == owner,
                                                 sa.func.lower(sa.func.trim(strategies.c.name)) == NAME.lower())
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
