"""52 Candle Breakout 15 MIN onto weekly candles, as 52 Week High Breakout (29 Oct)

Agreed 29 Oct, after the daily / weekly / monthly backtests: the strategy
trades the same 500 stocks on weekly candles built from the stored daily
ones -- a finished week closing above the highest high of the 52 before it
is bought at 09:16 on the next week's first trading day (strongest first,
up to 10), and sold at 09:16 on the trading day after a finished week
closes below its own 10-week MA of (O+H+L+C)/4. The stocks version 1
bought on 15-minute candles are sold at the first 09:16 after the switch,
and that first run also ranks the last finished week's breakouts -- on
Thu 29 Oct, deployed before the open: the week ending Fri 23 Oct, bought
at 09:16 that day (native_strategies/week_high_breakout_52.py; TO_MD5).

Each strategy whose latest version is exactly version 1's code (FROM_MD5S)
gets the new code as its next version (timeframe "1wk"), and its active or
paused paper run on that code moves onto it, keeping its state -- the
holdings the new code sells. The strategy is renamed "52 Week High
Breakout", with its description, and so is its pool where it still has
the old name. A strategy edited by hand since is left alone, and nothing
happens if the built-in isn't this code. Recorded in audit_logs.

Revision ID: f5b7d9e1a3c6
Revises: e3a5c7d9f1b4
Create Date: 2026-10-29 08:45:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7d9e1a3c6"
down_revision: Union[str, None] = "e3a5c7d9f1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/week_high_breakout_52.py"
FROM_MD5S = {"4f45f84aff0e9f0b4eb15d02d8245cbe"}  # 52 Candle Breakout 15 MIN, 28 Oct (e3a5c7d9f1b4): version 1
TO_MD5 = "1ff9f26da9ac7f736c6e4c115cbf168c"
OLD_NAME, NEW_NAME = "52 Candle Breakout 15 MIN", "52 Week High Breakout"
DESCRIPTION = ("500 NSE stocks, weekly: buy a week's close above the 52-week high at the next week's open (strongest first, "
               "up to 10), sell after a week's close below its 10-week MA of OHLC/4 (29 Oct)")
TIMEFRAME = "1wk"
REASON = "weekly candles, renamed 52 Week High Breakout (agreed 29 Oct)"

strategies = sa.table(
    "strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String), sa.column("description", sa.String),
    sa.column("owner_id", sa.Uuid),
)
versions = sa.table(
    "strategy_versions", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("version_number", sa.Integer),
    sa.column("timeframe", sa.String), sa.column("instrument_ids", sa.JSON), sa.column("parameters", sa.JSON),
    sa.column("entry_rules", sa.JSON), sa.column("exit_rules", sa.JSON), sa.column("python_code", sa.Text),
    sa.column("position_sizing", sa.JSON), sa.column("risk_rules", sa.JSON), sa.column("created_by", sa.Uuid),
)
portfolios = sa.table("paper_portfolios", sa.column("id", sa.Uuid), sa.column("name", sa.String))
deployments = sa.table(
    "paper_native_deployments", sa.column("id", sa.Uuid), sa.column("portfolio_id", sa.Uuid), sa.column("strategy_id", sa.Uuid),
    sa.column("strategy_version_id", sa.Uuid), sa.column("status", sa.String),
)
live_runs = sa.table(
    "live_native_deployments", sa.column("id", sa.Uuid), sa.column("paper_deployment_id", sa.Uuid),
    sa.column("strategy_version_id", sa.Uuid), sa.column("status", sa.String),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def _md5(code: str | None) -> str:
    return hashlib.md5((code or "").replace("\r", "").encode()).hexdigest()


def _audit(bind, action: str, object_type: str, object_id, previous: dict, new: dict, user_id=None) -> None:
    bind.execute(audit_logs.insert().values(
        id=uuid.uuid4(), user_id=user_id, action=action, object_type=object_type, object_id=str(object_id),
        previous_value=previous, new_value=new,
    ))


def upgrade() -> None:
    if not BUILT_IN.is_file():
        return
    new_code = BUILT_IN.read_text(encoding="utf-8")
    if _md5(new_code) != TO_MD5:
        return  # the built-in has changed since: not the code approved here
    bind = op.get_bind()
    copied = ("strategy_id", "version_number", "timeframe", "instrument_ids", "parameters", "entry_rules", "exit_rules",
              "python_code", "position_sizing", "risk_rules", "created_by")
    newest = (
        sa.select(versions.c.strategy_id, sa.func.max(versions.c.version_number).label("n"))
        .group_by(versions.c.strategy_id).subquery()
    )
    latest = bind.execute(
        sa.select(*(versions.c[name] for name in copied))
        .join(newest, (newest.c.strategy_id == versions.c.strategy_id) & (newest.c.n == versions.c.version_number))
        .where(versions.c.python_code.like(f"%{OLD_NAME}%"))
    ).mappings().all()
    for row in latest:
        if _md5(row["python_code"]) not in FROM_MD5S:
            continue
        new_id = uuid.uuid4()
        bind.execute(versions.insert().values(
            id=new_id, strategy_id=row["strategy_id"], version_number=row["version_number"] + 1, timeframe=TIMEFRAME,
            instrument_ids=row["instrument_ids"], parameters=row["parameters"], entry_rules=row["entry_rules"],
            exit_rules=row["exit_rules"], python_code=new_code, position_sizing=row["position_sizing"],
            risk_rules=row["risk_rules"], created_by=row["created_by"],
        ))
        _audit(bind, "STRATEGY_VERSION_CREATED", "strategy", row["strategy_id"], {"version_number": row["version_number"]},
               {"version_number": row["version_number"] + 1, "reason": REASON})

        strategy = bind.execute(
            sa.select(strategies.c.name, strategies.c.owner_id).where(strategies.c.id == row["strategy_id"])
        ).mappings().first()
        if strategy is not None and strategy["name"].strip().lower() == OLD_NAME.lower():
            bind.execute(strategies.update().where(strategies.c.id == row["strategy_id"]).values(name=NEW_NAME, description=DESCRIPTION))
            _audit(bind, "STRATEGY_RENAMED", "strategy", row["strategy_id"], {"name": strategy["name"]}, {"name": NEW_NAME},
                   strategy["owner_id"])

        cards = bind.execute(
            sa.select(deployments.c.id, deployments.c.portfolio_id, deployments.c.status, versions.c.id.label("version_id"),
                      versions.c.version_number, versions.c.python_code)
            .join(versions, versions.c.id == deployments.c.strategy_version_id)
            .where(deployments.c.strategy_id == row["strategy_id"])
        ).mappings().all()
        for card in cards:
            pool = bind.execute(sa.select(portfolios.c.name).where(portfolios.c.id == card["portfolio_id"])).scalar()
            if pool is not None and pool.strip().lower() == OLD_NAME.lower():
                bind.execute(portfolios.update().where(portfolios.c.id == card["portfolio_id"]).values(name=NEW_NAME))
                _audit(bind, "PAPER_PORTFOLIO_RENAMED", "paper_portfolio", card["portfolio_id"], {"name": pool}, {"name": NEW_NAME},
                       strategy["owner_id"] if strategy is not None else None)
            if card["status"] not in ("active", "paused") or _md5(card["python_code"]) not in FROM_MD5S:
                continue  # stopped, or on another version of its own: left there
            bind.execute(deployments.update().where(deployments.c.id == card["id"]).values(strategy_version_id=new_id))
            bind.execute(live_runs.update().where(
                live_runs.c.paper_deployment_id == card["id"], live_runs.c.strategy_version_id == card["version_id"],
                live_runs.c.status.in_(("active", "paused")),
            ).values(strategy_version_id=new_id))
            _audit(bind, "PAPER_NATIVE_VERSION_UPDATED", "paper_native_deployment", card["id"],
                   {"version_number": card["version_number"]}, {"version_number": row["version_number"] + 1, "reason": REASON})


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back; rename from the app
