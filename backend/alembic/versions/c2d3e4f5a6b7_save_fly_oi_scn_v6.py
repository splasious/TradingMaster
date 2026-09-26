"""Save FLY OI SCN version 6 as a new strategy version and run it

Asked for by the user (26 Sep 2026): the strategy's code lives in the
database (strategy_versions.python_code), so the version 6 code shipped in
app/services/strategy/native_strategies/fo_opening_momentum.py only runs
once it's saved as a version. This saves it as the next version of the
strategy whose active paper deployment runs FLY OI SCN (settings copied
from its current version), points that deployment at it and clears the
deployment's day state -- the same as saving in the editor and restarting
the deployment. Written to the audit log. A no-op if there's no such
strategy; if a version 6 is already saved, the deployment is just pointed
at it.

To go back: point the deployment's strategy_version_id at the previous
version again.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-26 00:00:00.000000

"""
import json
import uuid
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'c2d3e4f5a6b7'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CODE_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "strategy" / "native_strategies" / "fo_opening_momentum.py"
MARKER = "%FLY OI SCN%"
V6_MARKER = "%VERSION = 6%"


def _json(value):
    """A JSON column read through raw SQL can come back as its text."""
    return json.loads(value) if isinstance(value, str) else value


def upgrade() -> None:
    bind = op.get_bind()
    code = CODE_PATH.read_text(encoding="utf-8")
    if "VERSION = 6" not in code:
        return

    deployments = bind.execute(sa.text(
        "SELECT d.id AS deployment_id, d.strategy_version_id, sv.strategy_id "
        "FROM paper_native_deployments d JOIN strategy_versions sv ON sv.id = d.strategy_version_id "
        "WHERE d.status = 'active' AND sv.python_code LIKE :marker"
    ), {"marker": MARKER}).mappings().all()

    for strategy_id in dict.fromkeys(d["strategy_id"] for d in deployments):
        strategy = bind.execute(sa.text("SELECT owner_id FROM strategies WHERE id = :id"), {"id": strategy_id}).mappings().first()
        latest = bind.execute(sa.text(
            "SELECT * FROM strategy_versions WHERE strategy_id = :id ORDER BY version_number DESC LIMIT 1"
        ), {"id": strategy_id}).mappings().first()
        existing = bind.execute(sa.text(
            "SELECT id, version_number FROM strategy_versions WHERE strategy_id = :id AND python_code LIKE :v6 "
            "ORDER BY version_number DESC LIMIT 1"
        ), {"id": strategy_id, "v6": V6_MARKER}).mappings().first()

        if existing is not None:
            version_id, version_number = existing["id"], existing["version_number"]
        else:
            version_id, version_number = uuid.uuid4(), latest["version_number"] + 1
            bind.execute(sa.text(
                "INSERT INTO strategy_versions (id, strategy_id, version_number, timeframe, instrument_ids, parameters, "
                "entry_rules, exit_rules, python_code, position_sizing, risk_rules, created_by, created_at) "
                "VALUES (:id, :strategy_id, :version_number, :timeframe, :instrument_ids, :parameters, :entry_rules, "
                ":exit_rules, :python_code, :position_sizing, :risk_rules, :created_by, now())"
            ).bindparams(
                sa.bindparam("instrument_ids", type_=sa.JSON), sa.bindparam("parameters", type_=sa.JSON),
                sa.bindparam("entry_rules", type_=sa.JSON), sa.bindparam("exit_rules", type_=sa.JSON),
                sa.bindparam("position_sizing", type_=sa.JSON), sa.bindparam("risk_rules", type_=sa.JSON),
            ), {
                "id": version_id, "strategy_id": strategy_id, "version_number": version_number,
                "timeframe": latest["timeframe"], "instrument_ids": _json(latest["instrument_ids"]) or [],
                "parameters": _json(latest["parameters"]) or {}, "entry_rules": _json(latest["entry_rules"]),
                "exit_rules": _json(latest["exit_rules"]), "python_code": code,
                "position_sizing": _json(latest["position_sizing"]) or {}, "risk_rules": _json(latest["risk_rules"]) or {},
                "created_by": strategy["owner_id"] if strategy else None,
            })

        for d in (d for d in deployments if d["strategy_id"] == strategy_id):
            bind.execute(sa.text(
                "UPDATE paper_native_deployments SET strategy_version_id = :version_id, state = NULL WHERE id = :id"
            ), {"version_id": version_id, "id": d["deployment_id"]})
            bind.execute(sa.text(
                "INSERT INTO audit_logs (id, user_id, action, object_type, object_id, previous_value, new_value, created_at) "
                "VALUES (:id, :user_id, 'STRATEGY_VERSION_CREATED', 'paper_native_deployment', :object_id, :previous, :new, now())"
            ).bindparams(sa.bindparam("previous", type_=sa.JSON), sa.bindparam("new", type_=sa.JSON)), {
                "id": uuid.uuid4(), "user_id": strategy["owner_id"] if strategy else None, "object_id": str(d["deployment_id"]),
                "previous": {"strategy_version_id": str(d["strategy_version_id"])},
                "new": {"strategy_version_id": str(version_id), "version_number": version_number,
                        "note": "FLY OI SCN v6 saved from the built-in code and deployed (migration c2d3e4f5a6b7)"},
            })


def downgrade() -> None:
    pass
