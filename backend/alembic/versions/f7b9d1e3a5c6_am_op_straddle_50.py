"""AM OP TRD 15 MIN: the straddle on a 50-point strike, the one with the higher CE + PE premium

Agreed 16 Oct: the short straddle no longer sells the nearest 100-point
strike but, of the two 50-point strikes either side of NIFTY, the one whose
CE + PE premium is the higher (the nearer on a tie). The spreads keep their
100-point strikes; the roll, PCR exits, MA exit and 15:00 are unchanged.

As with d5f7b9c1e3a4, the running card's code (version 9, FROM_MD5S) is
only in the app's database, so the change goes in as EDITS, applied only if
each required one finds exactly one place, the names it adds aren't taken,
and the result compiles with the names it uses. The strategy named
STRATEGY_NAME whose latest version is that code gets the edited code as its
next version, and its active or paused paper run on it moves onto it,
keeping its state, with its live run if it has one. The built-in is
unchanged: AM OP TRAD 15 MIN H keeps the 100-point straddle. Recorded in
audit_logs.

Revision ID: f7b9d1e3a5c6
Revises: e6a8c0d2f4b5
Create Date: 2026-10-16 11:00:00.000000

"""
import ast
import hashlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7b9d1e3a5c6"
down_revision: Union[str, None] = "e6a8c0d2f4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGY_NAME = "AM OP TRD 15 MIN"
FROM_MD5S = {"7d346130e740eb8239822b2864467a65"}  # the running version 9 (version 6 + the MA exit)
REASON = "the straddle on the 50-point strike with the higher CE + PE premium (agreed 16 Oct)"
OPTIONAL_EDITS = {0}  # the docstring's note: applied when its place is found
ADDED_NAMES = ("_straddle_strike", "STRADDLE_STEP")  # must not be taken already
# What the added code uses from evaluate(): the edited code must still assign them there.
REQUIRED_NAMES = {"underlying", "spot", "regime", "expiry", "atm"}
# (old, new): applied in order, each to exactly one place.
EDITS = [
    ('Entry: 9:45 AM IST onward.',
     'Straddle strike (16-Oct-2026): of the two STRADDLE_STEP (50) point strikes\neither side of NIFTY, the one whose CE + PE premium is the higher -- the\nnearer on a tie, the one priced if only one has both premiums live\n(_straddle_strike). The spreads keep their 100-point strikes.\n\nEntry: 9:45 AM IST onward.'),
    ('def determine_regime(pcr: float) -> str:',
     'STRADDLE_STEP = 50  # the straddle\'s strikes (16 Oct); the spreads keep STRIKE_STEP\n\n\nasync def _straddle_strike(ctx, underlying, expiry, spot: float) -> float | None:\n    """Of the two STRADDLE_STEP strikes either side of `spot`, the one whose\n    CE + PE premium is the higher -- the nearer on a tie; None when neither\n    has both premiums live."""\n    lower = (spot // STRADDLE_STEP) * STRADDLE_STEP\n    best = None\n    for strike in (lower, lower + STRADDLE_STEP):\n        total = 0.0\n        for option_type in ("CE", "PE"):\n            instrument = await ctx.find_option(underlying.id, expiry, strike, option_type)\n            price = await ctx.get_price(instrument.id) if instrument is not None else None\n            if price is None:\n                total = None\n                break\n            total += price\n        if total is not None and (best is None or (total, -abs(strike - spot)) > best[0]):\n            best = ((total, -abs(strike - spot)), strike)\n    return best[1] if best is not None else None\n\n\ndef determine_regime(pcr: float) -> str:'),
    ('    atm = round_to_nearest_100(spot)\n',
     '    atm = round_to_nearest_100(spot)\n    if regime == "sideways":\n        atm = await _straddle_strike(ctx, underlying, expiry, spot)\n        if atm is None:\n            ctx.note("skipped", reason="no live CE + PE premium at either 50-point strike around NIFTY yet")\n            return\n'),
]

versions = sa.table(
    "strategy_versions", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("version_number", sa.Integer),
    sa.column("timeframe", sa.String), sa.column("instrument_ids", sa.JSON), sa.column("parameters", sa.JSON),
    sa.column("entry_rules", sa.JSON), sa.column("exit_rules", sa.JSON), sa.column("python_code", sa.Text),
    sa.column("position_sizing", sa.JSON), sa.column("risk_rules", sa.JSON), sa.column("created_by", sa.Uuid),
)
strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
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


def edited(code: str) -> str | None:
    """`code` with EDITS applied, or None when a required one has no single
    place, or the result doesn't compile or lacks a name the added code uses."""
    code = code.replace("\r", "")
    if any(name in code for name in ADDED_NAMES):
        return None
    for i, (old, new) in enumerate(EDITS):
        if code.count(old) == 1:
            code = code.replace(old, new)
        elif i not in OPTIONAL_EDITS:
            return None
    try:
        compile(code, "<am-op>", "exec")
        tree = ast.parse(code)
    except SyntaxError:
        return None
    evaluate = next((n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "evaluate"), None)
    if evaluate is None:
        return None
    assigned = {n.name for n in ast.walk(evaluate) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for node in ast.walk(evaluate):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
        assigned |= {t.id for t in targets if isinstance(t, ast.Name)}
    return code if REQUIRED_NAMES <= assigned else None


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
        .join(strategies, strategies.c.id == versions.c.strategy_id)
        .where(sa.func.trim(strategies.c.name) == STRATEGY_NAME)
    ).mappings().all()
    for row in latest:
        if _md5(row["python_code"]) not in FROM_MD5S:
            continue
        new_code = edited(row["python_code"])
        if new_code is None:
            continue  # the code isn't what the edits were written for: left alone
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
            if _md5(run["python_code"]) not in FROM_MD5S:
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
    pass  # "Use latest version" on a re-saved version 9 switches back
