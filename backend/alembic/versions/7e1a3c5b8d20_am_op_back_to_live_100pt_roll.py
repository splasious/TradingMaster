"""AM OP TRD 15 MIN: back to rolling on the live 100-point move (version 6, unchanged)

Decided 5 Oct evening, after comparing the day both ways: keep the original
rollover -- the moment live NIFTY is 100 points from entry -- rather than the
15-minute-close rule version 7 brought in at 15:39 the same day (it never
traded: the market had closed). Each active or paused AM OP deployment still
running exactly version 7 (md5 V7_MD5) gets its strategy's version 6 code
(md5 V6_MD5), copied unchanged, as the next version, and moves onto it,
keeping its state; its live run, if on, moves with it. Nothing else is
touched -- a deployment on any other code, or a strategy without that
version 6, is left alone. Recorded in audit_logs.

Revision ID: 7e1a3c5b8d20
Revises: 5c8e1f2a9b3d
Create Date: 2026-10-05 18:10:00.000000

"""
import hashlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7e1a3c5b8d20"
down_revision: Union[str, None] = "5c8e1f2a9b3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

V6_MD5 = "93a247f5b3c48d565d8ea652c192324d"  # saved 22 Sep 19:13 -- rolls on the live 100-point move
V7_MD5 = "10139b9db333d2766f69a465126bd5af"  # 5 Oct 15:39 -- rolls on a 15-minute close
STRATEGY_NAME = "AM OP TRD 15 MIN"

strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
versions = sa.table(
    "strategy_versions", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("version_number", sa.Integer),
    sa.column("timeframe", sa.String), sa.column("instrument_ids", sa.JSON), sa.column("parameters", sa.JSON),
    sa.column("entry_rules", sa.JSON), sa.column("exit_rules", sa.JSON), sa.column("python_code", sa.Text),
    sa.column("position_sizing", sa.JSON), sa.column("risk_rules", sa.JSON), sa.column("created_by", sa.Uuid),
)
deployments = sa.table(
    "paper_native_deployments", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid),
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


def upgrade() -> None:
    bind = op.get_bind()
    copied = ("strategy_id", "version_number", "timeframe", "instrument_ids", "parameters", "entry_rules", "exit_rules",
              "python_code", "position_sizing", "risk_rules", "created_by")
    rows = bind.execute(
        sa.select(deployments.c.id.label("deployment_id"), versions.c.id.label("version_id"), *(versions.c[name] for name in copied))
        .join(versions, versions.c.id == deployments.c.strategy_version_id)
        .join(strategies, strategies.c.id == deployments.c.strategy_id)
        .where(deployments.c.status.in_(("active", "paused")), sa.func.trim(strategies.c.name) == STRATEGY_NAME)
    ).mappings().all()
    for row in rows:
        if _md5(row["python_code"]) != V7_MD5:
            continue
        older = bind.execute(
            sa.select(versions.c.python_code).where(versions.c.strategy_id == row["strategy_id"])
            .order_by(versions.c.version_number.desc())
        ).scalars().all()
        v6_code = next((code for code in older if _md5(code) == V6_MD5), None)
        if v6_code is None:
            continue  # no unchanged version 6 to go back to: left as it is
        latest = bind.execute(
            sa.select(sa.func.max(versions.c.version_number)).where(versions.c.strategy_id == row["strategy_id"])
        ).scalar_one()
        new_id = uuid.uuid4()
        bind.execute(versions.insert().values(
            id=new_id, strategy_id=row["strategy_id"], version_number=latest + 1, timeframe=row["timeframe"],
            instrument_ids=row["instrument_ids"], parameters=row["parameters"], entry_rules=row["entry_rules"],
            exit_rules=row["exit_rules"], python_code=v6_code, position_sizing=row["position_sizing"],
            risk_rules=row["risk_rules"], created_by=row["created_by"],
        ))
        bind.execute(deployments.update().where(deployments.c.id == row["deployment_id"]).values(strategy_version_id=new_id))
        bind.execute(live_runs.update().where(
            live_runs.c.paper_deployment_id == row["deployment_id"], live_runs.c.strategy_version_id == row["version_id"],
            live_runs.c.status.in_(("active", "paused")),
        ).values(strategy_version_id=new_id))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="PAPER_NATIVE_VERSION_UPDATED", object_type="paper_native_deployment",
            object_id=str(row["deployment_id"]), previous_value={"version_number": row["version_number"]},
            new_value={"version_number": latest + 1, "reason": "AM OP back to the live 100-point roll (version 6 code), decided 5 Oct"},
        ))


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved version 7 switches back
