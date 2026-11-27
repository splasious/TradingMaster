"""OHLC4 Buy 5 MIN: the strategy, its 4-lakh pool and its paper card (27 Nov).

A new built-in (native_strategies/nifty_ohlc4_buy.py), as agreed 27 Nov
after the backtest: checked at the 09:15 open (live NIFTY) and at each
5-minute close 09:20-15:25 (that candle's close) against the previous
15-minute NIFTY candle's (open + high + low + close) / 4 -- above it, one
lot of the ATM NIFTY weekly call held; below it, the put; bought at the
ask; the side changing, the held option sold at the bid and the other
bought 5 seconds later; the stop the 15-minute candle's low (call) / high
(put), no trailing stop; held overnight, rolled to next week's at 12:00 on
its expiry day; no brokerage. Created for the owner of Conti Calendar 15
MIN: the strategy with version 1 the built-in's code (timeframe 5m), its
own pool "OHLC4 Buy 5 MIN" of Rs 4,00,000, and an active paper card on it
-- no live run, no Telegram alerts. Nothing when that owner has a strategy
of this name already. Recorded in audit_logs.

Revision ID: a3c5e7b9d1f4
Revises: e6f8a0c2b4d7
Create Date: 2026-11-27 12:00:00.000000

"""

import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7b9d1f4"
down_revision: Union[str, None] = "e6f8a0c2b4d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = "OHLC4 Buy 5 MIN"
FROM_STRATEGY = "conti calendar 15 min"
CAPITAL = 400_000.0
TIMEFRAME = "5m"
BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_ohlc4_buy.py"
DESCRIPTION = ("NIFTY: 1 lot of the ATM weekly call held while NIFTY checks above the previous 15-minute candle's "
               "(O+H+L+C)/4, the put while below -- checked at the open and each 5-minute close, flipped 5 s after selling, "
               "stop the 15-minute candle's low / high, held overnight, rolled 12:00 on expiry day (27 Nov)")

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
            id=version_id, strategy_id=strategy_id, version_number=1, timeframe=TIMEFRAME, instrument_ids=[], parameters={},
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
