"""AM OP TRD 15 MIN: the spreads need 200 points of room to the 3 biggest-OI walls, as Nifty PCR Futures

Agreed 23 Oct, as Nifty PCR Futures' version 8 the same day: the bull put
spread (bullish) and the bear call spread (bearish) open -- a fresh entry or
a 100-point roll's reopen alike -- only when live NIFTY is WALL_ROOM (200)
points from each of the WALL_COUNT (3) strikes with the most open interest
of their side's option type (bearish: puts; bullish: calls), on EITHER side
of NIFTY, of the expiry they sell, in the 15-minute record the PCR comes
from. A wall NIFTY has just broken keeps blocking until other strikes' OI
pushes it out of the top 3. A roll whose reopen is blocked has still closed
the old spread: the card stays flat and enters again once there's room. The
straddle, the PCR exits, the MA exit, the roll trigger and 15:00 are
unchanged.

As with f7b9d1e3a5c6, the running card's code (FROM_MD5S) is only in the
app's database, so the change goes in as EDITS, applied only if each
required one finds exactly one place, the names it adds aren't taken, and
the result compiles with the names it uses. The strategy named
STRATEGY_NAME whose latest version is that code gets the edited code as its
next version, and its active or paused paper run on it moves onto it,
keeping its state, with its live run if it has one -- an open spread carries
on; the rule applies to its next entry or roll. The built-in is unchanged:
AM OP TRAD 15 MIN H keeps opening as before. Recorded in audit_logs.

Revision ID: d1f3a5c7e9b2
Revises: c9e1a3b5d7f2
Create Date: 2026-10-23 12:30:00.000000

"""
import ast
import hashlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3a5c7e9b2"
down_revision: Union[str, None] = "c9e1a3b5d7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGY_NAME = "AM OP TRD 15 MIN"
FROM_MD5S = {"9139f121a22e606cfab13b0e1c448765"}  # the running code: version 6 + the MA exit + the 50-point straddle
REASON = "the spreads need 200 points of room to the 3 biggest-OI walls, on either side of NIFTY (agreed 23 Oct)"
OPTIONAL_EDITS = {0}  # the docstring's note: applied when its place is found
ADDED_NAMES = ("_oi_walls", "WALL_ROOM", "WALL_COUNT")  # must not be taken already
# What the added code uses from evaluate(): the edited code must still assign them there.
REQUIRED_NAMES = {"regime", "spot", "expiry", "pcr", "is_rollover"}
# (old, new): applied in order, each to exactly one place.
EDITS = [
    ('Entry: 9:45 AM IST onward.',
     'Room to the walls (23-Oct-2026, as Nifty PCR Futures): the bull put and\n'
     'bear call spreads open -- a fresh entry or a roll\'s reopen -- only with\n'
     'live NIFTY WALL_ROOM (200) points from each of the WALL_COUNT (3) strikes\n'
     'with the most OI of their side\'s option type (bearish: puts; bullish:\n'
     'calls), either side of NIFTY, of the expiry they sell, in the PCR\'s\n'
     '15-minute record (_oi_walls). A broken wall counts until its OI leaves\n'
     'the top 3. A blocked roll has closed the old spread: flat until there\'s\n'
     'room. The straddle is unchanged.\n\nEntry: 9:45 AM IST onward.'),
    ('def determine_regime(pcr: float) -> str:',
     'WALL_ROOM = 200  # NIFTY points a spread needs from each of the WALL_COUNT biggest-OI strikes of its side (23 Oct)\n'
     'WALL_COUNT = 3\n\n\n'
     'async def _oi_walls(ctx, regime: str, expiry, spot: float):\n'
     '    """(the WALL_COUNT strikes of `expiry` with the most OI of the spread\'s\n'
     '    side -- bearish: puts; bullish: calls -- either side of NIFTY, in the\n'
     '    15-minute record the PCR comes from, ""), or (None, why not)."""\n'
     '    from app.services.options.pcr_snapshots import _strike_map, latest_record\n\n'
     '    record = await latest_record(ctx.db, "NIFTY", ctx.now)\n'
     '    if record is None:\n'
     '        return None, "no 15-min OI record"\n'
     '    option_type = "PE" if regime == "bearish" else "CE"\n'
     '    picks = [(k, oi) for (e, k, t), oi in (await _strike_map(ctx.db, record.id, {})).items()\n'
     '             if e == expiry and t == option_type and oi is not None]\n'
     '    if not picks:\n'
     '        return None, f"no {expiry:%d %b} {option_type} OI in the latest 15-min record"\n'
     '    picks.sort(key=lambda p: (-p[1], abs(p[0] - spot)))\n'
     '    return [k for k, _ in picks[:WALL_COUNT]], ""\n\n\n'
     'def determine_regime(pcr: float) -> str:'),
    ('    atm = round_to_nearest_100(spot)\n',
     '    if regime in ("bullish", "bearish"):  # room to the option writers\' walls first (23 Oct)\n'
     '        walls, why = await _oi_walls(ctx, regime, expiry, spot)\n'
     '        near = [k for k in walls or [] if abs(k - spot) < WALL_ROOM]\n'
     '        if walls is None or near:\n'
     '            if near:\n'
     '                kind, wall = ("put" if regime == "bearish" else "call"), min(near, key=lambda k: abs(k - spot))\n'
     '                why = (f"{kind} OI wall {wall:,.0f} is {abs(spot - wall):,.0f} pts {\'above\' if wall > spot else \'below\'} "\n'
     '                       f"NIFTY {spot:,.1f} (needs {WALL_ROOM} from each of the top {WALL_COUNT} {kind} OI strikes: "\n'
     '                       f"{\', \'.join(f\'{k:,.0f}\' for k in sorted(walls))})")\n'
     '            if is_rollover:\n'
     '                ctx.note("exited", signal="COVER", reason=f"PCR {pcr:.3f}: {regime} spread rolled out, the reopen waits -- {why}")\n'
     '            else:\n'
     '                ctx.note("skipped", reason=f"PCR {pcr:.3f}: {regime} spread waits -- {why}")\n'
     '            return\n'
     '    atm = round_to_nearest_100(spot)\n'),
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
    pass  # "Use latest version" on a re-saved earlier version switches back
