"""RS Rotation 15 MIN: a slot with no candle takes the close before it

Since 3 Aug 2026 F&O stocks -- 43 of the 50 listed then -- have no candles
after 15:15, so the session's 15:15 candle never exists for them, and the
version before left them unranked every morning until they had ten live
bars of their own (12:00): the 09:30 "top 10" came out of the few others.
Now an empty slot takes the stock's close before it (native_strategies/
nifty_rs_rotation_15min.py, _seeded_series). Each active deployment running
exactly a version before (50 or 57 stocks, its code's md5 in FROM_MD5S)
gets the built-in code as its strategy's next version and moves onto it,
keeping its state; anything else is left alone, and nothing happens if the
built-in isn't this code (TO_MD5). Recorded in audit_logs.

Revision ID: 7dab5ef6e158
Revises: cc8323c6d54e
Create Date: 2026-10-01 06:10:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7dab5ef6e158"
down_revision: Union[str, None] = "cc8323c6d54e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGIES = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies"
# file: (FROM_MD5S -- the versions before, 50 and 57 stocks, TO_MD5 -- the one that carries a close forward)
CHANGES = {
    "nifty_rs_rotation_15min.py": (
        {"99da3a7283fd0afd59add3360cae2cdd", "5a9c766dbf2905e41410671e435753d5"}, "988246769bebae7d1d8c3cdfbcf91be8",
    ),
}

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
    active = bind.execute(
        sa.select(deployments.c.id.label("deployment_id"), *(versions.c[name] for name in copied))
        .join(versions, versions.c.id == deployments.c.strategy_version_id)
        .where(deployments.c.status == "active")
    ).mappings().all()
    for filename, (from_md5s, to_md5) in CHANGES.items():
        builtin = STRATEGIES / filename
        if not builtin.is_file():
            continue
        new_code = builtin.read_text(encoding="utf-8")
        if _md5(new_code) != to_md5:
            continue  # the built-in has changed since: not the code approved here
        for row in active:
            if _md5(row["python_code"]) not in from_md5s:
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
            bind.execute(audit_logs.insert().values(
                id=uuid.uuid4(), user_id=None, action="PAPER_NATIVE_VERSION_UPDATED", object_type="paper_native_deployment",
                object_id=str(row["deployment_id"]), previous_value={"version_number": row["version_number"]},
                new_value={"version_number": latest + 1, "reason": "an empty 15:15 slot takes the 15:00 close (F&O stocks have no 15:15 candle), 1 Oct"},
            ))


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back
