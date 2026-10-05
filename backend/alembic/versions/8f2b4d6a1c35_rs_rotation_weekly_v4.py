"""RS Rotation Weekly: the fixed weekly RS rotation as its next version

Agreed 6 Oct. Its backtests came back empty: the old code read daily
candles past the replayed day (so the ranking never changed and nothing was
ever sold) and never closed its holdings at the end. The new built-in
(native_strategies/nifty_rs_rotation.py, md5 NEW_MD5) also buys as soon as
it starts, readjusts Fridays at 15:00 to equal weights, and shifts a Friday
holiday to the trading day before. Saved as the next version of every
strategy whose latest version is still the old weekly code (OLD_NORM_MD5,
ignoring blank lines and trailing spaces). None is deployed, so nothing
running moves; a deployment of it would switch with "Use latest version".

Revision ID: 8f2b4d6a1c35
Revises: 7e1a3c5b8d20
Create Date: 2026-10-06 05:30:00.000000

"""
import hashlib
import uuid
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8f2b4d6a1c35"
down_revision: Union[str, None] = "7e1a3c5b8d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_NORM_MD5 = "ff02ad89e4b573f79fad3db1e8adc915"  # the weekly code saved 30 Sep, blank lines aside
NEW_MD5 = "b6eb9311fb569b044c27b39e3472eee7"
BUILT_IN = Path(__file__).resolve().parents[2] / "app" / "services" / "strategy" / "native_strategies" / "nifty_rs_rotation.py"

strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
versions = sa.table(
    "strategy_versions", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("version_number", sa.Integer),
    sa.column("timeframe", sa.String), sa.column("instrument_ids", sa.JSON), sa.column("parameters", sa.JSON),
    sa.column("entry_rules", sa.JSON), sa.column("exit_rules", sa.JSON), sa.column("python_code", sa.Text),
    sa.column("position_sizing", sa.JSON), sa.column("risk_rules", sa.JSON), sa.column("created_by", sa.Uuid),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def _norm_md5(code: str | None) -> str:
    lines = (line.rstrip() for line in (code or "").replace("\r", "").split("\n"))
    return hashlib.md5("\n".join(line for line in lines if line).encode()).hexdigest()


def upgrade() -> None:
    new_code = BUILT_IN.read_text(encoding="utf-8")
    if hashlib.md5(new_code.replace("\r", "").encode()).hexdigest() != NEW_MD5:
        return  # the built-in has moved on since: nothing to save from here
    bind = op.get_bind()
    copied = ("strategy_id", "version_number", "timeframe", "instrument_ids", "parameters", "entry_rules", "exit_rules",
              "python_code", "position_sizing", "risk_rules", "created_by")
    latest = (
        sa.select(versions.c.strategy_id, sa.func.max(versions.c.version_number).label("n"))
        .group_by(versions.c.strategy_id).subquery()
    )
    rows = bind.execute(
        sa.select(*(versions.c[name] for name in copied))
        .join(latest, (latest.c.strategy_id == versions.c.strategy_id) & (latest.c.n == versions.c.version_number))
        .where(versions.c.python_code.like("%_rebalance_date_for_week%"))
    ).mappings().all()
    for row in rows:
        if _norm_md5(row["python_code"]) != OLD_NORM_MD5:
            continue
        bind.execute(versions.insert().values(
            id=uuid.uuid4(), strategy_id=row["strategy_id"], version_number=row["version_number"] + 1, timeframe=row["timeframe"],
            instrument_ids=row["instrument_ids"], parameters=row["parameters"], entry_rules=row["entry_rules"],
            exit_rules=row["exit_rules"], python_code=new_code, position_sizing=row["position_sizing"],
            risk_rules=row["risk_rules"], created_by=row["created_by"],
        ))
        bind.execute(audit_logs.insert().values(
            id=uuid.uuid4(), user_id=None, action="STRATEGY_VERSION_CREATED", object_type="strategy",
            object_id=str(row["strategy_id"]), previous_value={"version_number": row["version_number"]},
            new_value={"version_number": row["version_number"] + 1,
                       "reason": "RS Rotation weekly fixed: backtests, buy on start, Friday 15:00 equal-weight readjust, holiday shift (agreed 6 Oct)"},
        ))


def downgrade() -> None:
    pass  # the earlier version stays saved; "Use latest version" and the editor work as ever
