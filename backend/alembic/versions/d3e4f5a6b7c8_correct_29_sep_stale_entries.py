"""Correct the 29 Sep 09:45 entries that read stale prices

Two advanced strategies entered at 09:45 IST with no live price on file
for the contract/stock yet, so the runner handed them an old one (see
native_runner get_price): AM OP TRD 15 MIN's two NIFTY option legs got
28 Sep's close, MACD - RSI - 15 MIN's ACUTAAS buy a price 1.8% below the
market. Each such entry becomes the real price at 09:45 -- the open of the
09:45 5-minute candle -- wherever it is recorded now: a closed trade
(whose P&L is recomputed), an open position's legs or an open holding.
The pool's cash moves by the difference (quantities are kept) and each
correction is written to audit_logs. Only entries still carrying the
known wrong price are touched, so it applies once; an entry whose real
candle isn't on file is left as it is.

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

OPENED_FROM = datetime(2026, 9, 29, 4, 15, 0, tzinfo=timezone.utc)  # 09:45:00 IST
OPENED_TO = datetime(2026, 9, 29, 4, 16, 0, tzinfo=timezone.utc)
CANDLE_TS = OPENED_FROM  # the 09:45 5-minute candle
# strategy name -> {symbol: the wrong entry price recorded}
WRONG_ENTRIES = {
    "AM OP TRD 15 MIN": {"NIFTY26O0622800CE": 206.60, "NIFTY26O0623000CE": 109.00},
    "MACD - RSI - 15 MIN": {"ACUTAAS": 3203.20},
}

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
            .where(bf_symbols.c.source.in_(["zerodha", "zerodha_nfo"]), bf_symbols.c.symbol == symbol,
                   bf_bars.c.timeframe == "5m", bf_bars.c.ts == CANDLE_TS)
        ).scalar()
    return round(float(price), 2) if price is not None else None


def _in_window(opened_at) -> bool:
    when = datetime.fromisoformat(opened_at) if isinstance(opened_at, str) else opened_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return OPENED_FROM <= when < OPENED_TO


def _correct(bind, legs: list[dict], wrong: dict[str, float]) -> tuple[list[dict], float, dict]:
    """(legs with real entries, cash change, {symbol: (was, now)}) -- legs
    not carrying a known wrong price, or without a real candle, unchanged."""
    ids = [uuid.UUID(str(leg["instrument_id"])) for leg in legs]
    symbols = {str(i): s for i, s in bind.execute(sa.select(instruments.c.id, instruments.c.symbol).where(instruments.c.id.in_(ids))).all()}
    new_legs, cash_change, changed = [], 0.0, {}
    for leg in legs:
        symbol = symbols.get(str(leg["instrument_id"]))
        was = wrong.get(symbol)
        now = _real_price(bind, symbol) if was is not None and abs(float(leg["entry_price"]) - was) < 0.001 else None
        if now is None:
            new_legs.append(leg)
            continue
        qty = float(leg["quantity"])
        # Opening a short credited qty x entry to cash; a buy (long leg, holding) debited it.
        cash_change += (now - was) * qty * (1 if leg.get("side") in ("short", "sell") else -1)
        changed[symbol] = (was, now)
        new_legs.append({**leg, "entry_price": now})
    return new_legs, cash_change, changed


def upgrade() -> None:
    bind = op.get_bind()
    for strategy_name, wrong in WRONG_ENTRIES.items():
        rows = bind.execute(
            sa.select(deployments.c.id, deployments.c.portfolio_id, deployments.c.state)
            .join(strategies, strategies.c.id == deployments.c.strategy_id).where(strategies.c.name == strategy_name)
        ).all()
        for deployment_id, portfolio_id, state in rows:
            user_id = bind.execute(sa.select(portfolios.c.user_id).where(portfolios.c.id == portfolio_id)).scalar()
            corrections = []  # (where, cash_change, changed)

            for trade_id, legs in bind.execute(
                sa.select(trades.c.id, trades.c.legs).where(
                    trades.c.deployment_id == deployment_id, trades.c.opened_at >= OPENED_FROM, trades.c.opened_at < OPENED_TO,
                )
            ).all():
                new_legs, cash_change, changed = _correct(bind, legs, wrong)
                if not changed:
                    continue
                pnl = sum(
                    ((leg["entry_price"] - leg["exit_price"]) if leg["side"] in ("short", "sell") else (leg["exit_price"] - leg["entry_price"]))
                    * leg["quantity"]
                    for leg in new_legs
                )
                notional = sum(leg["entry_price"] * leg["quantity"] for leg in new_legs)
                bind.execute(trades.update().where(trades.c.id == trade_id).values(
                    legs=new_legs, pnl=pnl, pnl_pct=(pnl / notional * 100) if notional else 0.0,
                ))
                corrections.append(("closed trade", cash_change, changed))

            state = state if isinstance(state, dict) else {}
            new_state = dict(state)
            position = state.get("position")
            if isinstance(position, dict) and isinstance(position.get("legs"), dict) and _in_window(position["opened_at"]):
                names = list(position["legs"])
                new_legs, cash_change, changed = _correct(bind, list(position["legs"].values()), wrong)
                if changed:
                    new_state["position"] = {**position, "legs": dict(zip(names, new_legs))}
                    corrections.append(("open position", cash_change, changed))
            holdings = state.get("holdings")
            if isinstance(holdings, dict):
                new_holdings = dict(holdings)
                for key, holding in holdings.items():
                    if isinstance(holding, dict) and holding.get("opened_at") and _in_window(holding["opened_at"]):
                        [fixed], cash_change, changed = _correct(bind, [holding], wrong)
                        if changed:
                            new_holdings[key] = fixed
                            corrections.append(("open holding", cash_change, changed))
                new_state["holdings"] = new_holdings
            if new_state != state:
                bind.execute(deployments.update().where(deployments.c.id == deployment_id).values(state=new_state))

            for where, cash_change, changed in corrections:
                bind.execute(portfolios.update().where(portfolios.c.id == portfolio_id).values(cash=portfolios.c.cash + cash_change))
                bind.execute(audit_logs.insert().values(
                    id=uuid.uuid4(), user_id=user_id, action="PAPER_NATIVE_ENTRY_CORRECTED", object_type="paper_native_deployment",
                    object_id=str(deployment_id), previous_value={"entries": {s: was for s, (was, _now) in changed.items()}},
                    new_value={"entries": {s: now for s, (_was, now) in changed.items()}, "cash_change": round(cash_change, 2),
                               "corrected": where, "reason": "09:45 entry read a stale price; set to the real 09:45 5m open"},
                ))


def downgrade() -> None:
    pass  # the wrong prices aren't worth restoring
