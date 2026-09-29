"""Correct AM OP TRD 15 MIN's option prices that weren't real (25-29 Sep)

d3e4f5a6b7c8 looked for each wrong entry's 5-minute candle, but NIFTY
weekly options are backfilled as 15-minute candles only, so it found none
for AM OP TRD 15 MIN's legs and changed nothing there. Every price below
was made in the first 5 minutes of a 15-minute candle, where that candle's
open is the 5-minute candle's open too, and lies outside the 15-minute
candle's low-high range, so it was never a real price in that candle
(health check Q3b/Q6):

- 29 Sep 09:45 IST: the bearish call spread's two entries
- 29 Sep 11:15 IST: the sideways straddle's two entries
- 25 Sep 14:04 IST: the morning trade's two exits and the next trade's two
  entries (about double the real prices)
- 28 Sep 15:00 IST: one exit, 0.8% outside the range

Each becomes the real price when it was made -- the candle's open (the
5-minute candle's if one is on file, else the 15-minute one's) -- in the
closed trade, with P&L, P&L % and estimated charges recomputed. The pool's
cash moves by the difference (quantities kept) and each correction goes to
audit_logs. A price inside its candle's range, or whose candle isn't on
file, is left as it is, so a second run changes nothing.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-09-29 21:00:00.000000

"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC = timezone.utc
STRATEGY = "AM OP TRD 15 MIN"
# starts (UTC) of the 15-minute candles whose first 5 minutes the wrong prices were made in
CANDLES = [
    datetime(2026, 9, 25, 8, 30, tzinfo=UTC),  # 25 Sep 14:00 IST: exits 14:04:18, entries 14:04:16
    datetime(2026, 9, 28, 9, 30, tzinfo=UTC),  # 28 Sep 15:00 IST: exit 15:00:10
    datetime(2026, 9, 29, 4, 15, tzinfo=UTC),  # 29 Sep 09:45 IST: entries 09:45:08
    datetime(2026, 9, 29, 5, 45, tzinfo=UTC),  # 29 Sep 11:15 IST: entries 11:15:23
]
WINDOW = timedelta(minutes=5)

strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
deployments = sa.table(
    "paper_native_deployments", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("portfolio_id", sa.Uuid),
)
trades = sa.table(
    "paper_native_trades", sa.column("id", sa.Uuid), sa.column("deployment_id", sa.Uuid),
    sa.column("opened_at", sa.DateTime(timezone=True)), sa.column("closed_at", sa.DateTime(timezone=True)),
    sa.column("legs", sa.JSON), sa.column("pnl", sa.Float), sa.column("pnl_pct", sa.Float), sa.column("charges", sa.Float),
)
portfolios = sa.table("paper_portfolios", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("cash", sa.Float))
instruments = sa.table("instruments", sa.column("id", sa.Uuid), sa.column("symbol", sa.String))
candles = sa.table(
    "ohlcv_candles", sa.column("instrument_id", sa.Uuid), sa.column("timeframe", sa.String),
    sa.column("ts", sa.DateTime(timezone=True)), sa.column("open", sa.Float), sa.column("low", sa.Float), sa.column("high", sa.Float),
)
bf_symbols = sa.table("bf_symbols", sa.column("id", sa.Uuid), sa.column("source", sa.String), sa.column("symbol", sa.String))
bf_bars = sa.table(
    "bf_ohlcv_bars", sa.column("symbol_id", sa.Uuid), sa.column("timeframe", sa.String),
    sa.column("ts", sa.DateTime(timezone=True)), sa.column("open", sa.Float), sa.column("low", sa.Float), sa.column("high", sa.Float),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _candle_start(when: datetime) -> datetime | None:
    when = _aware(when)
    return next((ts for ts in CANDLES if ts <= when < ts + WINDOW), None)


def _real_candle(bind, instrument_id: uuid.UUID, symbol: str, ts: datetime) -> tuple[float, float, float] | None:
    """(open, low, high): the 5-minute candle starting at ts if on file, else the 15-minute one -- chart table first,
    then the backfill's own copy."""
    for timeframe in ("5m", "15m"):
        row = bind.execute(
            sa.select(candles.c.open, candles.c.low, candles.c.high)
            .where(candles.c.instrument_id == instrument_id, candles.c.timeframe == timeframe, candles.c.ts == ts)
        ).first()
        if row is None:
            row = bind.execute(
                sa.select(bf_bars.c.open, bf_bars.c.low, bf_bars.c.high).join(bf_symbols, bf_symbols.c.id == bf_bars.c.symbol_id)
                .where(bf_symbols.c.source == "zerodha_nfo", bf_symbols.c.symbol == symbol,
                       bf_bars.c.timeframe == timeframe, bf_bars.c.ts == ts)
            ).first()
        if row is not None:
            return tuple(float(v) for v in row)
    return None


def _is_short(leg: dict) -> bool:
    return leg.get("side") in ("short", "sell")


def _charges(legs: list[dict], opened_at: datetime, closed_at: datetime, was: float | None) -> float | None:
    if was is None:
        return None
    try:
        from app.services.paper_trading.trade_record import estimate_charges

        now = estimate_charges(legs, opened_at, closed_at)
    except Exception:
        return was
    return was if now is None else now


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(deployments.c.id, deployments.c.portfolio_id)
        .join(strategies, strategies.c.id == deployments.c.strategy_id)
        .where(sa.func.trim(strategies.c.name) == STRATEGY)
    ).all()
    for deployment_id, portfolio_id in rows:
        user_id = bind.execute(sa.select(portfolios.c.user_id).where(portfolios.c.id == portfolio_id)).scalar()
        for trade_id, opened_at, closed_at, legs, charges in bind.execute(
            sa.select(trades.c.id, trades.c.opened_at, trades.c.closed_at, trades.c.legs, trades.c.charges)
            .where(trades.c.deployment_id == deployment_id)
        ).all():
            entry_ts, exit_ts = _candle_start(opened_at), _candle_start(closed_at)
            if (entry_ts is None and exit_ts is None) or not isinstance(legs, list):
                continue
            ids = [uuid.UUID(str(leg["instrument_id"])) for leg in legs if isinstance(leg, dict) and leg.get("instrument_id")]
            symbols = {str(i): s for i, s in bind.execute(sa.select(instruments.c.id, instruments.c.symbol).where(instruments.c.id.in_(ids))).all()}

            new_legs, cash_change = [], 0.0
            entries: dict[str, tuple[float, float]] = {}
            exits: dict[str, tuple[float, float]] = {}
            for leg in legs:
                symbol = symbols.get(str(leg.get("instrument_id"))) if isinstance(leg, dict) else None
                if symbol is None:
                    new_legs.append(leg)
                    continue
                fixed, qty = dict(leg), float(leg["quantity"])
                # Opening a short credited qty x entry to cash and closing it debited qty x exit; a long the reverse.
                for key, ts, found, sign in (("entry_price", entry_ts, entries, 1), ("exit_price", exit_ts, exits, -1)):
                    real = _real_candle(bind, uuid.UUID(str(leg["instrument_id"])), symbol, ts) if ts else None
                    was = float(leg[key])
                    if real is None or real[1] <= was <= real[2]:
                        continue
                    now = round(real[0], 2)
                    fixed[key] = now
                    found[symbol] = (was, now)
                    cash_change += (now - was) * qty * sign * (1 if _is_short(leg) else -1)
                new_legs.append(fixed)
            if not entries and not exits:
                continue

            pnl = sum(
                ((leg["entry_price"] - leg["exit_price"]) if _is_short(leg) else (leg["exit_price"] - leg["entry_price"])) * leg["quantity"]
                for leg in new_legs
            )
            notional = sum(leg["entry_price"] * leg["quantity"] for leg in new_legs)
            bind.execute(trades.update().where(trades.c.id == trade_id).values(
                legs=new_legs, pnl=pnl, pnl_pct=(pnl / notional * 100) if notional else 0.0,
                charges=_charges(new_legs, _aware(opened_at), _aware(closed_at), charges),
            ))
            bind.execute(portfolios.update().where(portfolios.c.id == portfolio_id).values(cash=portfolios.c.cash + cash_change))
            bind.execute(audit_logs.insert().values(
                id=uuid.uuid4(), user_id=user_id, action="PAPER_NATIVE_PRICE_CORRECTED", object_type="paper_native_deployment",
                object_id=str(deployment_id),
                previous_value={"entries": {s: was for s, (was, _now) in entries.items()}, "exits": {s: was for s, (was, _now) in exits.items()}},
                new_value={"entries": {s: now for s, (_was, now) in entries.items()}, "exits": {s: now for s, (_was, now) in exits.items()},
                           "cash_change": round(cash_change, 2), "trade_id": str(trade_id),
                           "reason": "price was outside the real candle's range; set to the real candle's open"},
            ))


def downgrade() -> None:
    pass  # the wrong prices aren't worth restoring
