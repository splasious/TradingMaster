"""MACD - RSI - 15 MIN: the live run copies its paper card's holdings once (30 Oct)

Agreed 30 Oct: the live run on Kotak Neo started flat while its paper card
held 5 stocks, and should hold what the paper card holds. A live run's
state["adopt"] -- the paper card's holdings -- is bought at its next check
ahead of any fresh up-cross, at the live price, sized as any buy, then held
and sold by the usual rules (native_strategies/macd_rsi_15min.py; TO_MD5).

Each strategy whose latest version is exactly version 18's code (FROM_MD5S)
gets the new code as its next version; its active or paused paper run on
that code moves onto it, keeping its state, with its live run. A live run
moved here that holds nothing -- no holdings in its state, no position at
the broker -- gets its paper card's holdings, in the card's order, as its
state["adopt"]. Deployed in market hours on 30 Oct, so the copy is bought
at the run's first check after the deploy. A strategy edited by hand
since is left alone, and nothing happens if the built-in isn't this code.
Recorded in audit_logs.

Revision ID: c2e4a6b8d0f1
Revises: f5b7d9e1a3c6
Create Date: 2026-10-30 11:00:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2e4a6b8d0f1"
down_revision: Union[str, None] = "f5b7d9e1a3c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/macd_rsi_15min.py"
FROM_MD5S = {"5f43c1d04e769861328c0e7b54ff2729"}  # version 18, on the paper card and its live run on 30 Oct
TO_MD5 = "b604a03fc3e8506c8c85f9f618430325"
REASON = "a live run copies its paper card's holdings once (agreed 30 Oct)"

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
    "live_native_deployments", sa.column("id", sa.Uuid), sa.column("owner_id", sa.Uuid), sa.column("paper_deployment_id", sa.Uuid),
    sa.column("strategy_version_id", sa.Uuid), sa.column("status", sa.String), sa.column("state", sa.JSON),
)
live_positions = sa.table("live_native_positions", sa.column("id", sa.Uuid), sa.column("deployment_id", sa.Uuid))
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def _md5(code: str | None) -> str:
    return hashlib.md5((code or "").replace("\r", "").encode()).hexdigest()


def _audit(bind, action: str, object_type: str, object_id, previous: dict | None, new: dict, user_id=None) -> None:
    bind.execute(audit_logs.insert().values(
        id=uuid.uuid4(), user_id=user_id, action=action, object_type=object_type, object_id=str(object_id),
        previous_value=previous, new_value=new,
    ))


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
        .where(versions.c.python_code.like("%MACD - RSI - 15 MIN%"))
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
        _audit(bind, "STRATEGY_VERSION_CREATED", "strategy", row["strategy_id"], {"version_number": row["version_number"]},
               {"version_number": row["version_number"] + 1, "reason": REASON})
        running = bind.execute(
            sa.select(deployments.c.id, deployments.c.state, versions.c.id.label("version_id"), versions.c.version_number,
                      versions.c.python_code)
            .join(versions, versions.c.id == deployments.c.strategy_version_id)
            .where(deployments.c.strategy_id == row["strategy_id"], deployments.c.status.in_(("active", "paused")))
        ).mappings().all()
        for run in running:
            if _md5(run["python_code"]) not in FROM_MD5S:
                continue  # on another version of its own: left there
            bind.execute(deployments.update().where(deployments.c.id == run["id"]).values(strategy_version_id=new_id))
            _audit(bind, "PAPER_NATIVE_VERSION_UPDATED", "paper_native_deployment", run["id"],
                   {"version_number": run["version_number"]}, {"version_number": row["version_number"] + 1, "reason": REASON})
            paper_holdings = list(((run["state"] or {}).get("holdings") or {}).keys())
            lives = bind.execute(
                sa.select(live_runs.c.id, live_runs.c.owner_id, live_runs.c.state)
                .where(live_runs.c.paper_deployment_id == run["id"], live_runs.c.strategy_version_id == run["version_id"],
                       live_runs.c.status.in_(("active", "paused")))
            ).mappings().all()
            for live in lives:
                state = dict(live["state"] or {})
                values = {"strategy_version_id": new_id}
                holds = bool(state.get("holdings")) or bind.execute(
                    sa.select(live_positions.c.id).where(live_positions.c.deployment_id == live["id"]).limit(1)
                ).first() is not None
                if paper_holdings and not holds and not state.get("adopt"):
                    values["state"] = {**state, "adopt": paper_holdings}
                bind.execute(live_runs.update().where(live_runs.c.id == live["id"]).values(**values))
                _audit(bind, "LIVE_NATIVE_VERSION_UPDATED", "live_native_deployment", live["id"], None,
                       {"version_number": row["version_number"] + 1, "reason": REASON,
                        "copies": paper_holdings if "state" in values else []}, live["owner_id"])


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved older version switches back
