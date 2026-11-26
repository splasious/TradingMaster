"""OI Change Buy 15 MIN: the side on the live candle; no brokerage on its trades (26 Nov).

Agreed 26 Nov (native_strategies/nifty_oi_change_buy.py; TO_MD5): from the
09:30 close to 15:29 the side follows the live candle -- the PCR tab's LIVE
row, its Put / Call change in OI since the latest 15-minute record -- the
moment it turns, instead of the last 2 closed candles' average at each close;
and its trades are charged no brokerage (as Kotak's intraday), the taxes and
fees as always. Everything else as version 7. Not backtested: no live
reading between the closes is stored.

Each strategy whose latest version is exactly version 7's code (FROM_MD5S)
gets the new code as its next version, and its active or paused paper run
on that code moves onto it, keeping its state -- an option held then is
kept, and followed live from the next check -- and so does a live run on
that paper run. A strategy edited by hand since is left alone, and nothing
happens if the built-in isn't this code. Recorded in audit_logs.

Revision ID: a8c0e2b4d6f9
Revises: f4d6b8a0c2e7
Create Date: 2026-11-26 14:00:00.000000

"""

import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8c0e2b4d6f9"
down_revision: Union[str, None] = "f4d6b8a0c2e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_oi_change_buy.py"
FROM_MD5S = {"6caa1be2612e96e61327571015f096bc"}  # held overnight, 25 Nov (a6c8e0b2d4f5): version 7
TO_MD5 = "883fff0ba57bb73f853ace653dfde562"
REASON = "the side on the live candle's change in OI, not at each close; no brokerage on its trades (agreed 26 Nov)"

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
        .where(versions.c.python_code.like("%OI Change Buy 15 MIN%"))
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
