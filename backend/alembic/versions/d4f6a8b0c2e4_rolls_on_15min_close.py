"""AM OP TRD 15 MIN, AM OP TRAD 15 MIN H and Conti Straddle 15 MIN roll on a completed 15-minute close

Agreed 22 Oct: like Conti Ladder Straddle 15 MIN, these three roll -- close
and reopen -- only when a 15-minute NIFTY candle completed since the
position opened (or last rolled) closes 100 points or more from the spot
recorded then; not the moment the live price is 100 points away (on 22 Oct
Conti rolled at 10:32 mid-candle on a live move the 10:30 candle closed 72
points from). Everything else is unchanged.

AM OP TRD 15 MIN and AM OP TRAD 15 MIN H: their running code is only in the
app's database (AM OP TRD's is version 6's code with the MA exit and the
50-point straddle; AM OP H's is the built-in, nifty_pcr_multi_regime.py),
so the change goes in as EDITS -- as d5f7b9c1e3a4 / f7b9d1e3a5c6 did --
applied only to code whose md5 is in FROM_MD5S, only if each required edit
finds exactly one place, the names it adds aren't taken, and the result
compiles, still assigns in evaluate() the names the added code uses and
defines at module level the helpers it calls (the MA exit's newest candle,
BAR, ma_is_current, as_aware_utc). The roll uses the candle the MA exit
already reads (_ma's newest), when it completed after the position opened
and NIFTY's candles are current; no roll otherwise. The built-in itself is
unchanged, as with f7b9d1e3a5c6, so earlier migrations' checks against it
still hold.

Conti Straddle 15 MIN: version 1 is the built-in of 21 Oct exactly
(CONTI_FROM_MD5); it gets today's built-in (nifty_conti_straddle.py,
CONTI_TO_MD5) as version 2.

Each such strategy's latest version on that code gets the new code as its
next version, and its active or paused paper run on that code moves onto
it, keeping its state (the open position carries on under the new rule),
with its live run if it has one. Anything else is left alone. Recorded in
audit_logs.

Revision ID: d4f6a8b0c2e4
Revises: c3e5a7b9d1f2
Create Date: 2026-10-22 11:15:00.000000

"""

import ast
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f6a8b0c2e4"
down_revision: Union[str, None] = "c3e5a7b9d1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REASON = "rolls on a completed 15-minute close 100 points from entry, not the live price (agreed 22 Oct)"

AM_OP_NAMES = ("AM OP TRD 15 MIN", "AM OP TRAD 15 MIN H")
AM_OP_FROM_MD5S = {
    "8243fb826f346aab68611b9d5573a8ea",  # the built-in with the MA exit: AM OP TRAD 15 MIN H
    "9139f121a22e606cfab13b0e1c448765",  # AM OP TRD 15 MIN's running version 12 (version 6 + the MA exit + the 50-point straddle)
}
OPTIONAL_EDITS = {0}  # the docstring's note: applied when its place is found
ADDED_NAMES = ("roll_close", "_roll_text")  # must not be taken already
# What the edited roll uses from evaluate(), and from the module.
REQUIRED_NAMES = {"newest", "now_ist", "position", "spot", "entry_spot", "regime"}
REQUIRED_MODULE_NAMES = {"BAR", "IST", "ROLL_TRIGGER", "ma_is_current", "as_aware_utc", "datetime"}
# (old, new): applied in order, each to exactly one place.
EDITS = [
    ('Rolling: closes and reopens the SAME regime at the new ATM once spot has\n'
     'moved ROLL_TRIGGER (100) points from the spot recorded when the position\n'
     'was last opened/rolled',
     'Rolling (22-Oct-2026: on a completed 15-minute close, not the live price):\n'
     'closes and reopens the SAME regime at the new ATM once a 15-minute NIFTY\n'
     'candle completed since the position was last opened/rolled closes\n'
     'ROLL_TRIGGER (100) points from the spot recorded then (roll_close)'),
    ('def _candle_text(',
     'def roll_close(newest: dict | None, opened_at: datetime, now_ist: datetime) -> dict | None:\n'
     '    """The completed 15-minute candle a roll is decided on (22 Oct): the newest,\n'
     '    when it finished after the position opened (or last rolled) and NIFTY\'s\n'
     '    candles are current; None otherwise -- no roll then."""\n'
     '    if newest is None:\n'
     '        return None\n'
     '    ended = as_aware_utc(newest["ts"]) + BAR\n'
     '    return newest if ended > opened_at and ma_is_current(ended, now_ist) else None\n'
     '\n'
     '\n'
     'def _roll_text(closed: dict | None, moved: float) -> str:\n'
     '    if closed is None:\n'
     '        return f"no 15-min close since it opened (rolls on one {ROLL_TRIGGER}pt from entry)"\n'
     '    ended = (as_aware_utc(closed["ts"]) + BAR).astimezone(IST)\n'
     '    return f"{ended:%H:%M} close {closed[\'close\']:,.1f} ({moved:.1f}pt from entry; rolls at {ROLL_TRIGGER})"\n'
     '\n'
     '\n'
     'def _candle_text('),
    ('        entry_spot = position.get("entry_spot")\n'
     '        moved = abs(spot - entry_spot) if entry_spot is not None else 0.0\n',
     '        entry_spot = position.get("entry_spot")\n'
     '        # Rolls on a completed 15-minute close (22 Oct), not the live price.\n'
     '        closed = roll_close(newest, datetime.fromisoformat(position["opened_at"]), now_ist)\n'
     '        moved = abs(closed["close"] - entry_spot) if entry_spot is not None and closed is not None else 0.0\n'),
    ('reason=f"{regime} position open, spot {spot} vs entry {entry_spot} ({moved:.1f}pt moved){ma_part}"',
     'reason=f"{regime} position open, spot {spot} vs entry {entry_spot}; {_roll_text(closed, moved)}{ma_part}"'),
]

CONTI_NAME = "Conti Straddle 15 MIN"
CONTI_FROM_MD5 = "03536e1f5a51afd155c25f286b3da832"  # the built-in of 21 Oct, version 1
CONTI_TO_MD5 = "0227f8de70be4ff1bc1edfd3fdfbd403"  # today's built-in
CONTI_BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_conti_straddle.py"

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


def _assigned(node) -> set[str]:
    names = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        targets = n.targets if isinstance(n, ast.Assign) else [n.target] if isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.For)) else []
        for target in targets:
            names |= {t.id for t in ast.walk(target) if isinstance(t, ast.Name)}
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in n.names}
    return names


def edited(code: str) -> str | None:
    """AM OP `code` with EDITS applied, or None when a required one has no
    single place, or the result doesn't compile or lacks a name it uses."""
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
    if evaluate is None or not REQUIRED_NAMES <= _assigned(evaluate):
        return None
    module = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)):
            module |= _assigned(node)
    return code if REQUIRED_MODULE_NAMES <= module else None


def _move(bind, row, new_code: str, from_md5s: set[str]) -> None:
    """`row` (a strategy's latest version) gets `new_code` as its next
    version; its active or paused runs on code in `from_md5s` move onto it."""
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


def _latest(bind, names: tuple[str, ...]):
    copied = ("strategy_id", "version_number", "timeframe", "instrument_ids", "parameters", "entry_rules", "exit_rules",
              "python_code", "position_sizing", "risk_rules", "created_by")
    newest = (
        sa.select(versions.c.strategy_id, sa.func.max(versions.c.version_number).label("n"))
        .group_by(versions.c.strategy_id).subquery()
    )
    return bind.execute(
        sa.select(*(versions.c[name] for name in copied))
        .join(newest, (newest.c.strategy_id == versions.c.strategy_id) & (newest.c.n == versions.c.version_number))
        .join(strategies, strategies.c.id == versions.c.strategy_id)
        .where(sa.func.trim(strategies.c.name).in_(names))
    ).mappings().all()


def upgrade() -> None:
    bind = op.get_bind()
    for row in _latest(bind, AM_OP_NAMES):
        if _md5(row["python_code"]) not in AM_OP_FROM_MD5S:
            continue
        new_code = edited(row["python_code"])
        if new_code is not None:
            _move(bind, row, new_code, AM_OP_FROM_MD5S)

    if CONTI_BUILT_IN.is_file():
        conti = CONTI_BUILT_IN.read_text(encoding="utf-8")
        if _md5(conti) == CONTI_TO_MD5:
            for row in _latest(bind, (CONTI_NAME,)):
                if _md5(row["python_code"]) == CONTI_FROM_MD5:
                    _move(bind, row, conti, {CONTI_FROM_MD5})


def downgrade() -> None:
    pass  # "Use latest version" on a re-saved earlier version switches back
