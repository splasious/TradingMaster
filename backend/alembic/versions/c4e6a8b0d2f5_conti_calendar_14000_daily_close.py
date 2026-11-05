"""Conti Calendar 15 MIN: take-profit Rs 14,000 and a 15:00 close every day (5 Nov).

Agreed 5 Nov: the take-profit falls from Rs 50,000 to Rs 14,000 -- read as
before, what closing all four legs now would give at bid / ask after the
charges of all eight orders -- and when it hasn't come by 15:00, all four
close then, every day: nothing is carried overnight
(native_strategies/nifty_conti_calendar.py; TO_MD5). Version 1 carried the
position overnight and closed it at 15:00 only on the weekly's expiry day.

Each strategy whose latest version is exactly version 1's code (FROM_MD5S)
gets the new code as its next version, and its active or paused paper run
on that code moves onto it, keeping its state -- an open position stays
open under the new rules, its target shown as Rs 14,000. A strategy edited
by hand since is left alone, and nothing happens if the built-in isn't this
code. Recorded in audit_logs.

Revision ID: c4e6a8b0d2f5
Revises: f8b0d2e4a6c9
Create Date: 2026-11-05 10:30:00.000000

"""

import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e6a8b0d2f5"
down_revision: Union[str, None] = "f8b0d2e4a6c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_conti_calendar.py"
FROM_MD5S = {"b4044a52c90e5206558251ec17b9c409"}  # Rs 50,000, held overnight, 4 Nov (f8b0d2e4a6c9): version 1
TO_MD5 = "f2c9c3d70a4b4458814a2200dd84c7a6"
TAKE_PROFIT = 14_000.0
REASON = "take-profit Rs 14,000 at bid/ask after charges, and all four legs closed at 15:00 every day (agreed 5 Nov)"

versions = sa.table(
    "strategy_versions", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("version_number", sa.Integer),
    sa.column("timeframe", sa.String), sa.column("instrument_ids", sa.JSON), sa.column("parameters", sa.JSON),
    sa.column("entry_rules", sa.JSON), sa.column("exit_rules", sa.JSON), sa.column("python_code", sa.Text),
    sa.column("position_sizing", sa.JSON), sa.column("risk_rules", sa.JSON), sa.column("created_by", sa.Uuid),
)
deployments = sa.table(
    "paper_native_deployments", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid),
    sa.column("strategy_version_id", sa.Uuid), sa.column("status", sa.String), sa.column("state", sa.JSON),
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
        .where(versions.c.python_code.like("%Conti Calendar 15 MIN%"))
    ).mappings().all()
    for row in latest:
        if _md5(row["python_code"]) not in FROM_MD5S:
            continue
        new_id = uuid.uuid4()
        bind.execute(versions.insert().values(
            id=new_id, strategy_id=row["strategy_id"], version_number=row["version_number"] + 1, timeframe=row["timeframe"],
            instrument_ids=row["instrument_ids"], parameters=row["parameters"], entry_rules=row["entry_rules"],
            exit_rules=row["exit_rules"], python_code=new_code, position_sizing=row["position_sizing"],
            risk_rules=row["risk_rules"], created_by=row["created_by"],
        ))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="STRATEGY_VERSION_CREATED", object_type="strategy",
            object_id=str(row["strategy_id"]), previous_value={"version_number": row["version_number"]},
            new_value={"version_number": row["version_number"] + 1, "reason": REASON},
        ))
        running = bind.execute(
            sa.select(deployments.c.id, deployments.c.state, versions.c.id.label("version_id"), versions.c.version_number,
                      versions.c.python_code)
            .join(versions, versions.c.id == deployments.c.strategy_version_id)
            .where(deployments.c.strategy_id == row["strategy_id"], deployments.c.status.in_(("active", "paused")))
        ).mappings().all()
        for run in running:
            if _md5(run["python_code"]) not in FROM_MD5S:
                continue  # on another version of its own: left there
            state = dict(run["state"] or {})
            position = state.get("position")
            values = {"strategy_version_id": new_id}
            if isinstance(position, dict) and "take_profit" in position:
                state["position"] = {**position, "take_profit": TAKE_PROFIT}  # what the card shows; the code reads its own
                values["state"] = state
            bind.execute(deployments.update().where(deployments.c.id == run["id"]).values(**values))
            bind.execute(live_runs.update().where(
                live_runs.c.paper_deployment_id == run["id"], live_runs.c.strategy_version_id == run["version_id"],
                live_runs.c.status.in_(("active", "paused")),
            ).values(strategy_version_id=new_id))
            bind.execute(audit_logs.insert().values(
                id=uuid.uuid4(), user_id=None, action="PAPER_NATIVE_VERSION_UPDATED", object_type="paper_native_deployment",
                object_id=str(run["id"]), previous_value={"version_number": run["version_number"]},
                new_value={"version_number": row["version_number"] + 1, "reason": REASON,
                           "open_position_kept": isinstance(position, dict)},
            ))


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back
