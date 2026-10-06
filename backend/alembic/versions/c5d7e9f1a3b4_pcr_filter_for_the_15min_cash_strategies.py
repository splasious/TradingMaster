"""RS Rotation 15 MIN and MACD - RSI - 15 MIN: the NIFTY PCR filter

Agreed 6 Oct. Both now check NIFTY's PCR (the 15-minute record over 4
expiries): below 0.80 they sell everything and stay out; above 0.90 they
buy again (RS: its top 10 at the next close; MACD: only on a fresh up-cross,
as ever); in between, or with no record, they stay as they were. Before the
first PCR record (backtests of earlier dates) there's no filter. See each
built-in's "PCR filter" section.

Each strategy whose latest version is exactly the code before (its md5 in
FROM_MD5S -- the 57-stock MACD of 1 Oct, the RS that carries a close into an
empty slot) gets the built-in code as its next version (TO_MD5), and its
active or paused paper run on that code moves onto it, keeping its state,
with its live run if it has one. A strategy edited by hand since is left
alone, and nothing happens if the built-in isn't this code. Recorded in
audit_logs.

Revision ID: c5d7e9f1a3b4
Revises: 9b3d5f7a2c46
Create Date: 2026-10-06 14:00:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5d7e9f1a3b4"
down_revision: Union[str, None] = "9b3d5f7a2c46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGIES = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies"
# file: (FROM_MD5S -- the version running before, TO_MD5 -- the one with the PCR filter)
CHANGES = {
    "macd_rsi_15min.py": ({"561759bbee3e25378f16c524c7f2f07b"}, "5f43c1d04e769861328c0e7b54ff2729"),
    "nifty_rs_rotation_15min.py": ({"988246769bebae7d1d8c3cdfbcf91be8"}, "92c7c0d9723cb8962692b9e39bb0166b"),
}
REASON = "NIFTY PCR filter: below 0.80 sell all and stay out, back in above 0.90 (agreed 6 Oct)"

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
    newest = (
        sa.select(versions.c.strategy_id, sa.func.max(versions.c.version_number).label("n"))
        .group_by(versions.c.strategy_id).subquery()
    )
    latest = bind.execute(
        sa.select(*(versions.c[name] for name in copied))
        .join(newest, (newest.c.strategy_id == versions.c.strategy_id) & (newest.c.n == versions.c.version_number))
    ).mappings().all()
    for filename, (from_md5s, to_md5) in CHANGES.items():
        builtin = STRATEGIES / filename
        if not builtin.is_file():
            continue
        new_code = builtin.read_text(encoding="utf-8")
        if _md5(new_code) != to_md5:
            continue  # the built-in has changed since: not the code approved here
        for row in latest:
            if _md5(row["python_code"]) not in from_md5s:
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
                sa.select(deployments.c.id, versions.c.id.label("version_id"), versions.c.version_number, versions.c.python_code)
                .join(versions, versions.c.id == deployments.c.strategy_version_id)
                .where(deployments.c.strategy_id == row["strategy_id"], deployments.c.status.in_(("active", "paused")))
            ).mappings().all()
            for run in running:
                if _md5(run["python_code"]) not in from_md5s:
                    continue  # on another version of its own: left there
                bind.execute(deployments.update().where(deployments.c.id == run["id"]).values(strategy_version_id=new_id))
                bind.execute(live_runs.update().where(
                    live_runs.c.paper_deployment_id == run["id"], live_runs.c.strategy_version_id == run["version_id"],
                    live_runs.c.status.in_(("active", "paused")),
                ).values(strategy_version_id=new_id))
                bind.execute(audit_logs.insert().values(
                    id=uuid.uuid4(), user_id=None, action="PAPER_NATIVE_VERSION_UPDATED", object_type="paper_native_deployment",
                    object_id=str(run["id"]), previous_value={"version_number": run["version_number"]},
                    new_value={"version_number": row["version_number"] + 1, "reason": REASON},
                ))


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back
