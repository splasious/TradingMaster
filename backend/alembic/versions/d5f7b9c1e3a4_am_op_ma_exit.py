"""AM OP TRD 15 MIN: the spreads also close on a 15-minute candle close across the MA

Agreed 15 Oct, as for Nifty PCR Futures: the bull put spread closes when a
15-minute NIFTY 50 candle completed after it opened closes below the MA --
the last 8 completed 15-minute candles' (O+H+L+C)/4, that candle included --
and the bear call spread when one closes above it; after that, the spread
opens again only once a later candle closes back on its side, the PCR still
in its zone (that day only). The straddle has no MA exit. PCR exits, the
100-point roll and the 15:00 exit are unchanged.

The running card's code (FROM_MD5S, version 8 -- version 6's code, saved in
the app) isn't in this repository, so the change goes in as EDITS: exact
replacements that turn the built-in of 23 Sep into today's built-in
(nifty_pcr_multi_regime.py, TO_MD5 -- tests/test_tonight_data_migrations.py
checks they do). Each strategy named STRATEGY_NAME whose latest version is
that code gets the edited code as its next version -- only if every
required edit finds exactly one place in it -- and its active or paused
paper run on that code moves onto it, keeping its state, with its live run
if it has one. Anything else is left alone. Recorded in audit_logs.

Revision ID: d5f7b9c1e3a4
Revises: c4e6a8b0d2f3
Create Date: 2026-10-15 16:00:00.000000

"""
import ast
import hashlib
import pathlib
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5f7b9c1e3a4"
down_revision: Union[str, None] = "c4e6a8b0d2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGY_NAME = "AM OP TRD 15 MIN"
FROM_MD5S = {"93a247f5b3c48d565d8ea652c192324d"}  # version 6's code, the running version 8
BUILT_IN = pathlib.Path(__file__).resolve().parents[2] / "app/services/strategy/native_strategies/nifty_pcr_multi_regime.py"
TO_MD5 = "8243fb826f346aab68611b9d5573a8ea"  # the built-in with the MA exit
REASON = "the spreads also close on a 15-minute candle close across the 8-candle MA (agreed 15 Oct)"
OPTIONAL_EDITS = {0}  # the docstring's note: applied when its place is found
# What the added code uses from evaluate(): the edited code must still assign them there.
REQUIRED_NAMES = {"now_utc", "now_ist", "underlying", "position", "spot", "regime", "pcr", "close_position", "is_rollover"}
# (old, new): applied in order, each to exactly one place.
EDITS = [
    ('      Exit when PCR rises back above 0.80.\n\n',
     '      Exit when PCR rises back above 0.80.\n\nMA exit (15-Oct-2026, as Nifty PCR Futures): the bull put spread also\ncloses when a 15-minute NIFTY 50 candle closes below the MA -- the average\nof the last MA_CANDLES (8) completed 15-minute candles\' (open + high + low\n+ close) / 4, including that candle -- and the bear call spread when one\ncloses above it. Only a candle completed after the position opened (or\nwas last rolled) counts, acted on when it comes in from Zerodha, within\nabout a minute of its close. The straddle has no MA exit. After one, that\nspread opens again only once a later 15-minute candle closes back on its\nside of the MA (bullish: above, bearish: below), the PCR still in its zone\n-- state["_ma_stop"], that day only. With fewer than 8 candles, or NIFTY\'s\ncandles behind (none newer than 30 minutes; at the open, the previous\nsession\'s last), there\'s no MA: no MA exit, and no wait.\n\n'),
    ('import uuid\nfrom datetime import datetime, time as dtime\n\n',
     'import uuid\nfrom datetime import date, datetime, time as dtime, timedelta\n\n'),
    ('\nfrom app.models.instrument import Instrument\n',
     '\nfrom app.core.time import as_aware_utc\nfrom app.models.instrument import Instrument\n'),
    ('from app.services.broker.zerodha_broker import IST\n\n',
     'from app.services.broker.zerodha_broker import IST\nfrom app.services.market_data.nse_holidays import is_trading_holiday\n\n'),
    ('SIDEWAYS_EXIT_HIGH = 1.25\n\n',
     'SIDEWAYS_EXIT_HIGH = 1.25\n\nMA_TIMEFRAME, MA_CANDLES = "15m", 8\nBAR = timedelta(minutes=15)\nMA_STALE_AFTER = timedelta(minutes=30)  # newest candle ended longer ago than this: no MA\nMARKET_OPEN = dtime(9, 15)\n\n'),
    ('def in_entry_window(t: dtime) -> bool:',
     'def _previous_trading_day(d: date) -> date:\n    d -= timedelta(days=1)\n    while d.weekday() >= 5 or is_trading_holiday(d):\n        d -= timedelta(days=1)\n    return d\n\n\ndef moving_average(bars: list[dict]) -> float | None:\n    """The MA of the last MA_CANDLES bars\' (open + high + low + close) / 4."""\n    if len(bars) < MA_CANDLES:\n        return None\n    return sum((b["open"] + b["high"] + b["low"] + b["close"]) / 4 for b in bars[-MA_CANDLES:]) / MA_CANDLES\n\n\ndef ma_is_current(newest_end: datetime, now_ist: datetime) -> bool:\n    """The newest candle ended at most MA_STALE_AFTER ago -- or, in the\n    session\'s first half hour, it is the previous session\'s last."""\n    newest_end = newest_end.astimezone(IST)\n    if newest_end.date() == now_ist.date():\n        return now_ist - newest_end <= MA_STALE_AFTER\n    opening = datetime.combine(now_ist.date(), MARKET_OPEN, tzinfo=IST)\n    return newest_end.date() == _previous_trading_day(now_ist.date()) and now_ist - opening <= MA_STALE_AFTER\n\n\ndef closed_against(regime: str, close: float, ma: float) -> bool:\n    """A close on the wrong side of the MA for this spread: under it for the\n    bull put spread, over it for the bear call spread."""\n    return close < ma if regime == "bullish" else close > ma\n\n\ndef ma_exit_due(regime: str, newest: dict, ma: float, opened_at: datetime) -> bool:\n    """The spread\'s MA exit: a candle completed since it opened closed against it."""\n    if regime not in ("bullish", "bearish"):\n        return False\n    ended = as_aware_utc(newest["ts"]) + BAR\n    return ended > opened_at and closed_against(regime, newest["close"], ma)\n\n\ndef re_armed(stop: dict, newest: dict | None, ma: float | None) -> bool:\n    """After an MA exit: a candle completed since closed back on the spread\'s side."""\n    if newest is None or ma is None:\n        return False\n    ended = as_aware_utc(newest["ts"]) + BAR\n    back = newest["close"] > ma if stop["regime"] == "bullish" else newest["close"] < ma\n    return ended > datetime.fromisoformat(stop["at"]) and back\n\n\nasync def _ma(ctx, underlying: Instrument, now_ist: datetime) -> tuple[float | None, dict | None]:\n    """(MA, the newest completed 15-minute candle); (None, ...) when there\'s no MA."""\n    bars = await ctx.get_candles(underlying.id, MA_TIMEFRAME, MA_CANDLES)\n    if len(bars) < MA_CANDLES:\n        return None, None\n    newest = bars[-1]\n    if not ma_is_current(as_aware_utc(newest["ts"]) + BAR, now_ist):\n        return None, newest\n    return moving_average(bars), newest\n\n\ndef _candle_text(newest: dict | None, ma: float | None) -> str:\n    if newest is None or ma is None:\n        return "no MA"\n    ended = (as_aware_utc(newest["ts"]) + BAR).astimezone(IST)\n    return f"{ended:%H:%M} close {newest[\'close\']:,.1f} vs MA {ma:,.1f}"\n\n\ndef in_entry_window(t: dtime) -> bool:'),
    ('    regime = None\n\n',
     '    regime = None\n    ma, newest = await _ma(ctx, underlying, now_ist)\n    stop = ctx.state.get("_ma_stop")\n    if stop is not None and datetime.fromisoformat(stop["at"]).astimezone(IST).date() != now_ist.date():\n        ctx.state.pop("_ma_stop")  # an MA exit holds back re-entry that day only\n        stop = None\n\n'),
    ('\n        entry_spot = position.get("entry_spot")\n',
     '\n        if ma is not None and ma_exit_due(regime, newest, ma, datetime.fromisoformat(position["opened_at"])):\n            await close_position("ma_exit")\n            ctx.state["_ma_stop"] = {"regime": regime, "at": now_utc.isoformat()}\n            ctx.note("exited", signal="COVER", reason=f"{regime} spread closed on the MA: {_candle_text(newest, ma)}")\n            return\n\n        entry_spot = position.get("entry_spot")\n'),
    ('        else:\n            ctx.note("hold", reason=f"{regime} position open, spot {spot} vs entry {entry_spot} ({moved:.1f}pt moved)")\n            return\n',
     '        else:\n            ma_part = ""\n            if regime in ("bullish", "bearish"):\n                ma_part = f" | last {_candle_text(newest, ma)} (MA exit on a 15-min close {\'below\' if regime == \'bullish\' else \'above\'} it)"\n            ctx.note("hold", reason=f"{regime} position open, spot {spot} vs entry {entry_spot} ({moved:.1f}pt moved){ma_part}")\n            return\n'),
    ('            ctx.note("skipped", reason=f"PCR {pcr:.3f} in a hysteresis buffer zone -- no edge either way")\n            return\n',
     '            ctx.note("skipped", reason=f"PCR {pcr:.3f} in a hysteresis buffer zone -- no edge either way")\n            return\n        if stop is not None and stop["regime"] == regime and not re_armed(stop, newest, ma):\n            stopped = datetime.fromisoformat(stop["at"]).astimezone(IST)\n            side = "above" if regime == "bullish" else "below"\n            ctx.note("skipped", reason=(\n                f"PCR {pcr:.3f}: {regime} closed on the MA at {stopped:%H:%M} -- waits for a 15-min close {side} it | "\n                f"last {_candle_text(newest, ma)}"\n            ))\n            return\n'),
    ('        await ctx.open_leg(leg["instrument"], leg["side"], quantity, leg["price"])\n\n',
     '        await ctx.open_leg(leg["instrument"], leg["side"], quantity, leg["price"])\n    if stop is not None and stop["regime"] == regime:\n        ctx.state.pop("_ma_stop", None)\n\n'),
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
    if not BUILT_IN.is_file() or _md5(BUILT_IN.read_text(encoding="utf-8")) != TO_MD5:
        return  # the built-in has changed since: not the change approved here
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
    pass  # "Use latest version" on a re-saved version 8 switches back
