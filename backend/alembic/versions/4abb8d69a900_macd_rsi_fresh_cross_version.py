"""MACD - RSI - 15 MIN: run the fresh up-cross buy rule

Approved 30 Sep: buy only when the newest finished 15-minute candle closes
with the MACD line above zero after the one before closed below it, highest
RSI first; exits unchanged (native_strategies/macd_rsi_15min.py). What
"Load built-in code", Save and "Use latest version" would do by hand: each
active deployment still running the version before it (exactly that code,
md5 OLD_MD5) gets the built-in code as its strategy's next version and moves
onto it, keeping its state -- holdings and all. A deployment on any other
code -- edited since -- is left alone. Recorded in audit_logs.

Revision ID: 4abb8d69a900
Revises: a6b7c8d9e0f1
Create Date: 2026-09-30 21:00:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4abb8d69a900"
down_revision: Union[str, None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_MD5 = "3e4a8b5ac9c85c20683a74159d793f86"  # the MACD-line version of 25 Sep
NEW_MD5 = "6a2b57d65304c397ea35bd56dcdee0ce"  # the fresh up-cross version of 30 Sep
BUILTIN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/macd_rsi_15min.py"

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
        sa.select(deployments.c.id.label("deployment_id"), *(versions.c[name] for name in copied))
        .join(versions, versions.c.id == deployments.c.strategy_version_id)
        .join(strategies, strategies.c.id == deployments.c.strategy_id)
        .where(deployments.c.status == "active", strategies.c.name.ilike("MACD%RSI%15%MIN%"))
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
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="PAPER_NATIVE_VERSION_UPDATED", object_type="paper_native_deployment",
            object_id=str(row["deployment_id"]), previous_value={"version_number": row["version_number"]},
            new_value={"version_number": latest + 1, "reason": "MACD fresh up-cross buy rule, approved 30 Sep"},
        ))


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back
