"""Nifty PCR Futures Hedge: intraday, entries with NIFTY 50 on the trade's side of its 8-candle MA, the MA as a trailing stop

Agreed 15 Oct (native_strategies/nifty_pcr_futures_hedge.py, TO_MD5):
bearish below PCR 0.90 with NIFTY 50 under the MA, out above 1.05; bullish
above 1.10 with NIFTY over the MA, out below 0.95. The MA -- the last 8
completed 15-minute NIFTY 50 candles' (open + high + low + close) / 4 -- is
also a trailing stop: NIFTY crossing it closes the trade, and that side is
entered again after a 15-minute close back across it. Entries 09:30-15:00,
everything closed at 15:00: nothing is held overnight, so no rolls.

Each strategy whose latest version is exactly the code of 7 Oct (FROM_MD5S,
e7f9a1b3c5d6) gets the new code as its next version -- what a backtest
runs. No run moves: a card on the code before stays on it until switched
with "Use latest version" (none runs as of 15 Oct). A strategy edited by
hand since is left alone, and nothing happens if the built-in isn't this
code. Recorded in audit_logs.

Revision ID: a2c4e6f8b0d1
Revises: f1b3d5e7a9c2
Create Date: 2026-10-15 13:00:00.000000

"""
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2c4e6f8b0d1"
down_revision: Union[str, None] = "f1b3d5e7a9c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_pcr_futures_hedge.py"
FROM_MD5S = {"eb8c82c34bbfcf02abd922e30ab1397a"}  # the built-in of 7 Oct (e7f9a1b3c5d6)
TO_MD5 = "06561cee37cbd919b73d3fc1136dfb03"
REASON = "intraday: PCR 0.90/1.05 and 1.10/0.95 with NIFTY 50's 8-candle MA to enter and as a trailing stop, all out at 15:00 (agreed 15 Oct)"

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
        .where(versions.c.python_code.like("%Nifty PCR Futures Hedge%"))
    ).mappings().all()
    for row in latest:
        if _md5(row["python_code"]) not in FROM_MD5S:
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
            new_value={"version_number": row["version_number"] + 1, "reason": REASON},
        ))


def downgrade() -> None:
    pass  # a card switches only with "Use latest version"
