"""Correct AM OP TRD 15 MIN's 29 Sep 09:45 entry prices

That entry read yesterday's prices: the two contracts had no live price
yet, so the runner fell back to 28 Sep's last close (see native_runner
get_price). Each leg's entry becomes the real price at 09:45 -- the open of
the 09:45 5-minute candle, 8 seconds before the entry -- and the recorded
trade's P&L and the pool's cash follow. Only legs still carrying the wrong
prices are touched, so it applies once; nothing changes if the real candle
isn't on file.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-09-29 12:00:00.000000

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STRATEGY_NAME = "AM OP TRD 15 MIN"
OPENED_FROM = datetime(2026, 9, 29, 4, 15, 0, tzinfo=timezone.utc)  # 09:45:00 IST
OPENED_TO = datetime(2026, 9, 29, 4, 16, 0, tzinfo=timezone.utc)
CANDLE_TS = OPENED_FROM  # the 09:45 5-minute candle
WRONG_ENTRY = {"NIFTY26O0622800CE": 206.60, "NIFTY26O0623000CE": 109.00}

strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
deployments = sa.table(
    "paper_native_deployments", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid),
    sa.column("portfolio_id", sa.Uuid), sa.column("state", sa.JSON),
)
trades = sa.table(
    "paper_native_trades", sa.column("id", sa.Uuid), sa.column("deployment_id", sa.Uuid),
    sa.column("opened_at", sa.DateTime(timezone=True)), sa.column("legs", sa.JSON),
    sa.column("pnl", sa.Float), sa.column("pnl_pct", sa.Float),
)
portfolios = sa.table("paper_portfolios", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("cash", sa.Float))
instruments = sa.table("instruments", sa.column("id", sa.Uuid), sa.column("symbol", sa.String))
candles = sa.table(
    "ohlcv_candles", sa.column("instrument_id", sa.Uuid), sa.column("timeframe", sa.String),
    sa.column("ts", sa.DateTime(timezone=True)), sa.column("open", sa.Float),
)
bf_symbols = sa.table("bf_symbols", sa.column("id", sa.Uuid), sa.column("source", sa.String), sa.column("symbol", sa.String))
bf_bars = sa.table(
    "bf_ohlcv_bars", sa.column("symbol_id", sa.Uuid), sa.column("timeframe", sa.String),
    sa.column("ts", sa.DateTime(timezone=True)), sa.column("open", sa.Float),
)
audit_logs = sa.table(
    "audit_logs", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("action", sa.String),
    sa.column("object_type", sa.String), sa.column("object_id", sa.String),
    sa.column("previous_value", sa.JSON), sa.column("new_value", sa.JSON),
)


def _real_price(bind, symbol: str) -> float | None:
    """The 09:45 5-minute candle's open, from the chart table or the backfill's own copy."""
    price = bind.execute(
        sa.select(candles.c.open).join(instruments, instruments.c.id == candles.c.instrument_id)
        .where(instruments.c.symbol == symbol, candles.c.timeframe == "5m", candles.c.ts == CANDLE_TS)
    ).scalar()
    if price is None:
        price = bind.execute(
            sa.select(bf_bars.c.open).join(bf_symbols, bf_symbols.c.id == bf_bars.c.symbol_id)
            .where(bf_symbols.c.source == "zerodha_nfo", bf_symbols.c.symbol == symbol,
                   bf_bars.c.timeframe == "5m", bf_bars.c.ts == CANDLE_TS)
        ).scalar()
    return round(float(price), 2) if price is not None else None


def _corrected_legs(bind, legs: list[dict], symbols: dict[str, str]) -> tuple[list[dict], float, dict] | None:
    """(legs with real entries, cash change, what changed), or None when there's nothing to correct."""
    real = {sym: _real_price(bind, sym) for sym in WRONG_ENTRY}
    if any(p is None for p in real.values()):
        return None
    new_legs, cash_change, changed = [], 0.0, {}
    for leg in legs:
        symbol = symbols.get(str(leg["instrument_id"]))
        wrong = WRONG_ENTRY.get(symbol)
        if wrong is None or abs(float(leg["entry_price"]) - wrong) > 0.001:
            new_legs.append(leg)
            continue
        fixed = real[symbol]
        qty = float(leg["quantity"])
        short = leg["side"] in ("short", "sell")
        # Opening a short credited qty x entry and a long debited it.
        cash_change += (fixed - wrong) * qty * (1 if short else -1)
        changed[symbol] = {"was": wrong, "now": fixed}
        new_legs.append({**leg, "entry_price": fixed})
    return (new_legs, cash_change, changed) if changed else None


def upgrade() -> None:
    bind = op.get_bind()
    deployment_rows = bind.execute(
        sa.select(deployments.c.id, deployments.c.portfolio_id, deployments.c.state)
        .join(strategies, strategies.c.id == deployments.c.strategy_id)
        .where(strategies.c.name == STRATEGY_NAME)
    ).all()
    for deployment_id, portfolio_id, state in deployment_rows:
        user_id = bind.execute(sa.select(portfolios.c.user_id).where(portfolios.c.id == portfolio_id)).scalar()
        trade_rows = bind.execute(
            sa.select(trades.c.id, trades.c.legs).where(
                trades.c.deployment_id == deployment_id, trades.c.opened_at >= OPENED_FROM, trades.c.opened_at < OPENED_TO,
            )
        ).all()
        targets = [("trade", trade_id, legs) for trade_id, legs in trade_rows]
        position = (state or {}).get("position") if isinstance(state, dict) else None
        if position and isinstance(position.get("legs"), dict):
            opened = datetime.fromisoformat(position["opened_at"])
            if OPENED_FROM <= opened < OPENED_TO:
                targets.append(("position", deployment_id, list(position["legs"].values())))
        for kind, object_id, legs in targets:
            ids = [uuid.UUID(str(leg["instrument_id"])) for leg in legs]
            symbols = {str(i): s for i, s in bind.execute(sa.select(instruments.c.id, instruments.c.symbol).where(instruments.c.id.in_(ids))).all()}
            result = _corrected_legs(bind, legs, symbols)
            if result is None:
                continue
            new_legs, cash_change, changed = result
            if kind == "trade":
                pnl = sum(
                    ((leg["entry_price"] - leg["exit_price"]) if leg["side"] in ("short", "sell") else (leg["exit_price"] - leg["entry_price"]))
                    * leg["quantity"]
                    for leg in new_legs
                )
                notional = sum(leg["entry_price"] * leg["quantity"] for leg in new_legs)
                bind.execute(trades.update().where(trades.c.id == object_id).values(
                    legs=new_legs, pnl=pnl, pnl_pct=(pnl / notional * 100) if notional else 0.0,
                ))
            else:
                names = list(position["legs"].keys())
                new_state = {**state, "position": {**position, "legs": dict(zip(names, new_legs))}}
                bind.execute(deployments.update().where(deployments.c.id == deployment_id).values(state=new_state))
            bind.execute(portfolios.update().where(portfolios.c.id == portfolio_id).values(cash=portfolios.c.cash + cash_change))
            bind.execute(audit_logs.insert().values(
                id=uuid.uuid4(), user_id=user_id, action="PAPER_NATIVE_ENTRY_CORRECTED", object_type="paper_native_deployment",
                object_id=str(deployment_id), previous_value={"entries": {s: c["was"] for s, c in changed.items()}},
                new_value={"entries": {s: c["now"] for s, c in changed.items()}, "cash_change": round(cash_change, 2),
                           "corrected": kind, "reason": "09:45 entry read 28 Sep's close; set to the real 09:45 5m open"},
            ))


def downgrade() -> None:
    pass  # the wrong prices aren't worth restoring
