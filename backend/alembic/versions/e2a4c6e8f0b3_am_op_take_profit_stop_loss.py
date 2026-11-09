"""AM OP TRD 15 MIN: a take-profit at +Rs 14,000 and a stop-loss at -Rs 10,000 on each position (9 Nov)

Agreed 9 Nov. While a position is open -- the straddle or either spread --
each check adds up its legs' P&L at their last traded prices, before
charges, as close_position books it and the card shows it; measured on
the position since it opened or was last rolled (a roll books its result
and the new one starts from Rs 0). At TAKE_PROFIT (+Rs 14,000) or more all
its legs close ("take_profit") and the next position opens at the next
check, the PCR giving a regime; at STOP_LOSS (Rs 10,000) of loss or more
they close ("stop_loss") and no new position opens until the next trading
day (state["_pnl_stop"]). With a leg unpriced there's no reading that
check. Checked before the MA exit and the roll; the PCR exits, the MA
exit, the roll, 15:00 and the strikes are unchanged.

As with d1f3a5c7e9b2, the running code (FROM_MD5S) is only in the app's
database, so the change goes in as EDITS, applied only if each required
one finds exactly one place, the names it adds aren't taken, and the
result compiles with the names it uses. The strategy named STRATEGY_NAME
whose latest version is that code gets the edited code as its next
version, and its active or paused paper run on it moves onto it, keeping
its state, with its live run if it has one -- an open position carries on
under the new rule. The built-in is unchanged: AM OP TRAD 15 MIN H keeps
trading as before. Recorded in audit_logs.

Revision ID: e2a4c6e8f0b3
Revises: d8f0a2c4e6b1
Create Date: 2026-11-09 10:30:00.000000

"""
import ast
import hashlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a4c6e8f0b3"
down_revision: Union[str, None] = "d8f0a2c4e6b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGY_NAME = "AM OP TRD 15 MIN"
FROM_MD5S = {"FROM_MD5_PENDING"}  # the running code: version 13, after d1f3a5c7e9b2's room to the OI walls
REASON = "a take-profit at +Rs 14,000 and a stop-loss at -Rs 10,000 on each position (agreed 9 Nov)"
OPTIONAL_EDITS = {0}  # the docstring's note: applied when its place is found
ADDED_NAMES = ("TAKE_PROFIT", "STOP_LOSS", "_open_pnl", "_pnl_stop")  # must not be taken already
# What the added code uses from evaluate(): the edited code must still assign them there.
REQUIRED_NAMES = {"regime", "position", "now_utc", "now_ist"}
# And from the module: the added code needs these imported at its top.
REQUIRED_IMPORTS = {"uuid", "datetime", "IST"}
# (old, new): applied in order, each to exactly one place.
EDITS = [
    ('Entry: 9:45 AM IST onward.',
     'Take-profit and stop-loss (9-Nov-2026): while a position is open, its legs\'\n'
     'P&L at their last prices, before charges, since it opened or was last\n'
     'rolled (_open_pnl): at +TAKE_PROFIT (Rs 14,000) all its legs close and the\n'
     'next opens at the next check; at -STOP_LOSS (Rs 10,000) they close and none\n'
     'opens again that day (state["_pnl_stop"]).\n\nEntry: 9:45 AM IST onward.'),
    ('def determine_regime(pcr: float) -> str:',
     'TAKE_PROFIT = 14_000.0  # rupees, each position\'s P&L at last prices before charges: close it (9 Nov)\n'
     'STOP_LOSS = 10_000.0  # rupees of loss, the same way: close it, and no new position that day (9 Nov)\n\n\n'
     'async def _open_pnl(ctx, position: dict) -> float | None:\n'
     '    """The open position\'s P&L at its legs\' last prices, before charges --\n'
     '    as close_position books it; None while a leg has no price."""\n'
     '    total = 0.0\n'
     '    for leg in position["legs"].values():\n'
     '        price = await ctx.get_price(uuid.UUID(leg["instrument_id"]))\n'
     '        if price is None:\n'
     '            return None\n'
     '        total += ((leg["entry_price"] - price) if leg["side"] == "sell" else (price - leg["entry_price"])) * leg["quantity"]\n'
     '    return total\n\n\n'
     'def determine_regime(pcr: float) -> str:'),
    ('        if ma is not None and ma_exit_due(regime, newest, ma, datetime.fromisoformat(position["opened_at"])):\n',
     '        open_pnl = await _open_pnl(ctx, position)  # take-profit / stop-loss first (9 Nov)\n'
     '        if open_pnl is not None and (open_pnl >= TAKE_PROFIT or open_pnl <= -STOP_LOSS):\n'
     '            hit = "take_profit" if open_pnl >= TAKE_PROFIT else "stop_loss"\n'
     '            await close_position(hit)\n'
     '            if hit == "stop_loss":\n'
     '                ctx.state["_pnl_stop"] = now_utc.isoformat()\n'
     '            ctx.note("exited", signal="COVER", reason=(\n'
     '                f"{regime} position closed at {\'take-profit\' if hit == \'take_profit\' else \'stop-loss\'}: P&L {open_pnl:+,.0f} "\n'
     '                f"at last prices (take-profit +{TAKE_PROFIT:,.0f}, stop-loss -{STOP_LOSS:,.0f})"\n'
     '                + ("; no new position today" if hit == "stop_loss" else "")\n'
     '            ))\n'
     '            return\n\n'
     '        if ma is not None and ma_exit_due(regime, newest, ma, datetime.fromisoformat(position["opened_at"])):\n'),
    ('    if not in_entry_window(now_ist.time()):\n',
     '    pnl_stop = ctx.state.get("_pnl_stop")  # a stop-loss holds back new positions that day (9 Nov)\n'
     '    if pnl_stop is not None:\n'
     '        stopped = datetime.fromisoformat(pnl_stop).astimezone(IST)\n'
     '        if stopped.date() == now_ist.date():\n'
     '            ctx.note("skipped", reason=f"stop-loss hit at {stopped:%H:%M:%S}: no new position until the next trading day")\n'
     '            return\n'
     '        ctx.state.pop("_pnl_stop")\n\n'
     '    if not in_entry_window(now_ist.time()):\n'),
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
    place, or the result doesn't compile, lacks a name the added code uses
    in evaluate(), or doesn't import what it needs."""
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
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {a.asname or a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name for a in node.names}
    if not REQUIRED_IMPORTS <= imported:
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
