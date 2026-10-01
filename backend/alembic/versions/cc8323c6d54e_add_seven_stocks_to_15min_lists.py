"""MACD - RSI - 15 MIN and RS Rotation 15 MIN: 7 stocks added to their list

Approved 1 Oct: CUPID, HFCL, KIRLOSENG, MTARTECH, STLTECH, TDPOWERSYS and
WELCORP join the 50 stocks both strategies track (WATCHLIST in
native_strategies/macd_rsi_15min.py, STOCK_UNIVERSE in
nifty_rs_rotation_15min.py) -- 57 each, nothing else changed. What "Load
built-in code", Save and "Use latest version" would do by hand: each active
deployment running exactly the version before (its code's md5 FROM_MD5)
gets the built-in code as its strategy's next version and moves onto it,
keeping its state -- holdings and all. A deployment on any other code --
edited since -- is left alone, and nothing happens if a built-in isn't the
code approved here (md5 TO_MD5). Recorded in audit_logs.

Revision ID: cc8323c6d54e
Revises: 3cd892aa3d34
Create Date: 2026-10-01 05:40:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "cc8323c6d54e"
down_revision: Union[str, None] = "3cd892aa3d34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGIES = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies"
# file: (FROM_MD5 -- the 50-stock version running, TO_MD5 -- the 57-stock one)
CHANGES = {
    "macd_rsi_15min.py": ("6a2b57d65304c397ea35bd56dcdee0ce", "561759bbee3e25378f16c524c7f2f07b"),
    "nifty_rs_rotation_15min.py": ("99da3a7283fd0afd59add3360cae2cdd", "5a9c766dbf2905e41410671e435753d5"),
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
    for filename, (from_md5, to_md5) in CHANGES.items():
        builtin = STRATEGIES / filename
        if not builtin.is_file():
            continue
        new_code = builtin.read_text(encoding="utf-8")
        if _md5(new_code) != to_md5:
            continue  # the built-in has changed since: not the code approved here
        for row in active:
            if _md5(row["python_code"]) != from_md5:
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
                new_value={"version_number": latest + 1, "reason": "7 stocks added to the 15-minute list (57), approved 1 Oct"},
            ))


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back
