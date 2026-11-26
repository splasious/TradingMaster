"""Synthetic Calendar 15 MIN and its Reverse: the hedge the weekly two weeks out, same strike (26 Nov).

Agreed 26 Nov (native_strategies/nifty_synthetic_calendar.py and
nifty_synthetic_calendar_reverse.py; TO_MD5S): the hedge -- the monthly
after the current weekly until now -- is the weekly two weeks after it, at
the same strike (e.g. this week's 1 Dec against the 15 Dec); everything else
as version 1. The open positions (monthly hedges) close at the first check
from 09:30 ("rule_change") and the new shape opens at once -- the
strategies do that themselves.

Each strategy whose latest version is exactly version 1's code of one of the
two (FROM_MD5S) gets that one's new code as its next version, and its active
or paused paper run on that code moves onto it, keeping its state, and so
does a live run on that paper run. A strategy edited by hand since is left
alone, and nothing happens for a built-in that isn't the approved code. A
description still the one it was created with (20 Nov) is brought up to
date. Recorded in audit_logs.

Revision ID: f4d6b8a0c2e7
Revises: d2b4f6a8c0e5
Create Date: 2026-11-26 13:00:00.000000

"""

import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4d6b8a0c2e7"
down_revision: Union[str, None] = "d2b4f6a8c0e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NATIVE = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies"
FROM_MD5S = {  # version 1's code (20 Nov, e0a2c4e6f8b1) -> the built-in that follows it
    "e0d721147e24c1d645183180d405c2cf": NATIVE / "nifty_synthetic_calendar.py",
    "3e54d82c71c29ea1ff19edf999c15c4a": NATIVE / "nifty_synthetic_calendar_reverse.py",
}
TO_MD5S = {"nifty_synthetic_calendar.py": "1f3213ef89492d5fec775150c2d84f32", "nifty_synthetic_calendar_reverse.py": "900e45680f84642c4189366a6a2a6ad0"}
DESCRIPTIONS = {  # as created 20 Nov -> now
    ("NIFTY at the ATM strike, 20 lots: weekly CE bought / PE sold, monthly PE bought / CE sold; Rs 14,000 take-profit "
     "after charges; closed 15:00 on the weekly's expiry day (20 Nov)"):
    ("NIFTY at the ATM strike, 20 lots: weekly CE bought / PE sold, the weekly two weeks out PE bought / CE sold; Rs 14,000 "
     "take-profit after charges; closed 15:00 on the weekly's expiry day (20 Nov; the hedge two weeks out 26 Nov)"),
    ("NIFTY at the ATM strike, 20 lots: weekly CE sold / PE bought, monthly PE sold / CE bought; Rs 14,000 take-profit "
     "after charges; closed 15:00 on the weekly's expiry day (20 Nov)"):
    ("NIFTY at the ATM strike, 20 lots: weekly CE sold / PE bought, the weekly two weeks out PE sold / CE bought; Rs 14,000 "
     "take-profit after charges; closed 15:00 on the weekly's expiry day (20 Nov; the hedge two weeks out 26 Nov)"),
}
REASON = "the hedge the weekly two weeks out at the same strike, not the monthly (agreed 26 Nov)"

strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("description", sa.String))
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
    bind = op.get_bind()
    new_code = {}
    for md5, path in FROM_MD5S.items():
        if path.is_file():
            code = path.read_text(encoding="utf-8")
            if _md5(code) == TO_MD5S[path.name]:
                new_code[md5] = code  # else the built-in has changed since: not the code approved here
    if not new_code:
        return
    copied = ("strategy_id", "version_number", "timeframe", "instrument_ids", "parameters", "entry_rules", "exit_rules",
              "python_code", "position_sizing", "risk_rules", "created_by")
    newest = (
        sa.select(versions.c.strategy_id, sa.func.max(versions.c.version_number).label("n"))
        .group_by(versions.c.strategy_id).subquery()
    )
    latest = bind.execute(
        sa.select(*(versions.c[name] for name in copied))
        .join(newest, (newest.c.strategy_id == versions.c.strategy_id) & (newest.c.n == versions.c.version_number))
        .where(versions.c.python_code.like("%Synthetic Calendar%15 MIN%"))
    ).mappings().all()
    for row in latest:
        old_md5 = _md5(row["python_code"])
        if old_md5 not in new_code:
            continue
        new_id = uuid.uuid4()
        bind.execute(versions.insert().values(
            id=new_id, strategy_id=row["strategy_id"], version_number=row["version_number"] + 1, timeframe=row["timeframe"],
            instrument_ids=row["instrument_ids"], parameters=row["parameters"], entry_rules=row["entry_rules"],
            exit_rules=row["exit_rules"], python_code=new_code[old_md5], position_sizing=row["position_sizing"],
            risk_rules=row["risk_rules"], created_by=row["created_by"],
        ))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="STRATEGY_VERSION_CREATED", object_type="strategy",
            object_id=str(row["strategy_id"]), previous_value={"version_number": row["version_number"]},
            new_value={"version_number": row["version_number"] + 1, "reason": REASON},
        ))
        description = bind.execute(sa.select(strategies.c.description).where(strategies.c.id == row["strategy_id"])).scalar()
        if description in DESCRIPTIONS:
            bind.execute(strategies.update().where(strategies.c.id == row["strategy_id"]).values(description=DESCRIPTIONS[description]))
        running = bind.execute(
            sa.select(deployments.c.id, deployments.c.state, versions.c.id.label("version_id"), versions.c.version_number,
                      versions.c.python_code)
            .join(versions, versions.c.id == deployments.c.strategy_version_id)
            .where(deployments.c.strategy_id == row["strategy_id"], deployments.c.status.in_(("active", "paused")))
        ).mappings().all()
        for run in running:
            if _md5(run["python_code"]) != old_md5:
                continue  # on another version of its own: left there
            position = (run["state"] or {}).get("position")
            bind.execute(deployments.update().where(deployments.c.id == run["id"]).values(strategy_version_id=new_id))
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
