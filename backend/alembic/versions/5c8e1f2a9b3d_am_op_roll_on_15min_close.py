"""AM OP TRD 15 MIN: roll only when a completed 15-minute candle closes 100+ points from entry

Approved 5 Oct: on 5 Oct the running version 6 rolled at 10:26:09 the moment
live NIFTY was 100 points from entry, mid-candle; from now on a roll is
decided only at a completed 15-minute close (10:00-14:45), on NIFTY at that
close (native_strategies/nifty_pcr_multi_regime.py). Entries, PCR exits and
the 15:00 exit are unchanged; the built-in also records the display-only
exit band, roll distance and last close on each position. What "Load
built-in code", Save and "Use latest version" would do by hand: each active
or paused deployment still running exactly version 6 (md5 OLD_MD5) gets the
built-in code as its strategy's next version and moves onto it, keeping its
state; a live run on that version moves with its paper card. A deployment on
any other code -- edited since -- is left alone. Recorded in audit_logs.

Revision ID: 5c8e1f2a9b3d
Revises: 4b7e2c9d1a60
Create Date: 2026-10-05 12:30:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5c8e1f2a9b3d"
down_revision: Union[str, None] = "4b7e2c9d1a60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_MD5 = "93a247f5b3c48d565d8ea652c192324d"  # version 6, saved 22 Sep 19:13 -- rolls on the live price
NEW_MD5 = "10139b9db333d2766f69a465126bd5af"  # rolls on completed 15-minute closes, 5 Oct
BUILTIN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_pcr_multi_regime.py"
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
    if not BUILTIN.is_file():
        return
    new_code = BUILTIN.read_text(encoding="utf-8")
    if _md5(new_code) != NEW_MD5:
        return  # the built-in has changed since: not the code approved here
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
        if _md5(row["python_code"]) != OLD_MD5:
            continue
        latest = bind.execute(
            sa.select(sa.func.max(versions.c.version_number)).where(versions.c.strategy_id == row["strategy_id"])
        ).scalar_one()
        new_id = uuid.uuid4()
        bind.execute(versions.insert().values(
            id=new_id, strategy_id=row["strategy_id"], version_number=latest + 1, timeframe=row["timeframe"],
            instrument_ids=row["instrument_ids"], parameters=row["parameters"], entry_rules=row["entry_rules"],
            exit_rules=row["exit_rules"], python_code=new_code, position_sizing=row["position_sizing"],
            risk_rules=row["risk_rules"], created_by=row["created_by"],
        ))
        bind.execute(deployments.update().where(deployments.c.id == row["deployment_id"]).values(strategy_version_id=new_id))
        # Its live run, if it's on, runs the card's version too.
        bind.execute(live_runs.update().where(
            live_runs.c.paper_deployment_id == row["deployment_id"], live_runs.c.strategy_version_id == row["version_id"],
            live_runs.c.status.in_(("active", "paused")),
        ).values(strategy_version_id=new_id))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="PAPER_NATIVE_VERSION_UPDATED", object_type="paper_native_deployment",
            object_id=str(row["deployment_id"]), previous_value={"version_number": row["version_number"]},
            new_value={"version_number": latest + 1, "reason": "AM OP rolls only on a completed 15-minute close, approved 5 Oct"},
        ))


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back
