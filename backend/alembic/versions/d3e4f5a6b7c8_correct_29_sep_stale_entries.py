"""Correct the entries that read stale prices (25-29 Sep)

These entries had no live price on file for the contract/stock yet, so the
runner handed them an old or simulated one (see native_runner get_price,
fixed in market_data/live_price.py): AM OP TRD 15 MIN's two NIFTY option
legs at 29 Sep 09:45 got 28 Sep's close, and its 11:15 straddle legs stale prices too; MACD - RSI - 15 MIN's first buys
of a stock -- the open holdings ACUTAAS (29 Sep 09:45), DELHIVERY
(29 Sep 10:31) and LAURUSLABS (28 Sep 11:30), and six since-closed trades
bought 25 Sep 09:15 and 12:15 and 28 Sep 12:00, 13:30 and 14:00 -- got
prices outside the real market range (health check Q4/Q5).

Each such entry becomes the real price when it was bought -- the open of
the 5-minute candle it was bought in -- wherever it is recorded now: a
closed trade (P&L recomputed), an open position's legs or an open holding.
The pool's cash moves by the difference (quantities kept) and each
correction goes to audit_logs. Only entries made inside the listed
5-minute candles are looked at, and one is only changed if its recorded
price lies outside that real candle's low-high range, so a correct price
is never touched and a second run changes nothing; an entry whose candle
isn't on file is left as it is.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-09-29 12:00:00.000000

"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c2d3e4f5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UTC = timezone.utc
# strategy name -> starts (UTC) of the 5-minute candles the wrong entries were made in
TARGETS = {
    "AM OP TRD 15 MIN": [
        datetime(2026, 9, 29, 4, 15, tzinfo=UTC),  # 29 Sep 09:45 IST: bearish call spread, both legs
        datetime(2026, 9, 29, 5, 45, tzinfo=UTC),  # 29 Sep 11:15 IST: sideways straddle, 22700 CE and PE
    ],
    "MACD - RSI - 15 MIN": [
        datetime(2026, 9, 25, 3, 45, tzinfo=UTC),  # 25 Sep 09:15 IST: closed trades 1 and 3
        datetime(2026, 9, 25, 6, 45, tzinfo=UTC),  # 25 Sep 12:15 IST: closed trade 8
        datetime(2026, 9, 28, 6, 0, tzinfo=UTC),  # 28 Sep 11:30 IST: LAURUSLABS
        datetime(2026, 9, 28, 6, 30, tzinfo=UTC),  # 28 Sep 12:00 IST: closed trade 11
        datetime(2026, 9, 28, 8, 0, tzinfo=UTC),  # 28 Sep 13:30 IST: closed trade 12
        datetime(2026, 9, 28, 8, 30, tzinfo=UTC),  # 28 Sep 14:00 IST: closed trade 13
        datetime(2026, 9, 29, 4, 15, tzinfo=UTC),  # 29 Sep 09:45 IST: ACUTAAS
        datetime(2026, 9, 29, 5, 0, tzinfo=UTC),  # 29 Sep 10:30 IST: DELHIVERY
    ],
}
CANDLE = timedelta(minutes=5)

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


def _real_candle(bind, symbol: str, ts: datetime) -> tuple[float, float, float] | None:
    """(open, low, high) of that 5-minute candle, from the chart table or the backfill's own copy."""
    row = bind.execute(
        sa.select(candles.c.open, candles.c.low, candles.c.high).join(instruments, instruments.c.id == candles.c.instrument_id)
        .where(instruments.c.symbol == symbol, candles.c.timeframe == "5m", candles.c.ts == ts)
    ).first()
    if row is None:
        row = bind.execute(
            sa.select(bf_bars.c.open, bf_bars.c.low, bf_bars.c.high).join(bf_symbols, bf_symbols.c.id == bf_bars.c.symbol_id)
            .where(bf_symbols.c.source.in_(["zerodha", "zerodha_nfo"]), bf_symbols.c.symbol == symbol,
                   bf_bars.c.timeframe == "5m", bf_bars.c.ts == ts)
        ).first()
    return tuple(float(v) for v in row) if row is not None else None


def _when(value) -> datetime | None:
    if value is None:
        return None
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _correct(bind, legs: list[dict], opened_at, targets: list[datetime]) -> tuple[list[dict], float, dict]:
    """(legs with real entries, cash change, {symbol: (was, now)})."""
    opened = _when(opened_at)
    candle_ts = next((ts for ts in targets if opened and ts <= opened < ts + CANDLE), None)
    if candle_ts is None:
        return legs, 0.0, {}
    ids = [uuid.UUID(str(leg["instrument_id"])) for leg in legs if leg.get("instrument_id")]
    symbols = {str(i): s for i, s in bind.execute(sa.select(instruments.c.id, instruments.c.symbol).where(instruments.c.id.in_(ids))).all()}
    new_legs, cash_change, changed = [], 0.0, {}
    for leg in legs:
        symbol = symbols.get(str(leg.get("instrument_id")))
        real = _real_candle(bind, symbol, candle_ts) if symbol else None
        was = float(leg["entry_price"])
        if real is None or real[1] <= was <= real[2]:
            new_legs.append(leg)
            continue
        now = round(real[0], 2)
        qty = float(leg["quantity"])
        # Opening a short credited qty x entry to cash; a buy (long leg, holding) debited it.
        cash_change += (now - was) * qty * (1 if leg.get("side") in ("short", "sell") else -1)
        changed[symbol] = (was, now)
        new_legs.append({**leg, "entry_price": now})
    return new_legs, cash_change, changed


def upgrade() -> None:
    bind = op.get_bind()
    for strategy_name, targets in TARGETS.items():
        rows = bind.execute(
            sa.select(deployments.c.id, deployments.c.portfolio_id, deployments.c.state)
            .join(strategies, strategies.c.id == deployments.c.strategy_id)
            .where(sa.func.trim(strategies.c.name) == strategy_name)  # the MACD one is stored with a trailing space
        ).all()
        earliest = min(targets)
        for deployment_id, portfolio_id, state in rows:
            user_id = bind.execute(sa.select(portfolios.c.user_id).where(portfolios.c.id == portfolio_id)).scalar()
            corrections = []  # (where, cash_change, changed)

            for trade_id, opened_at, legs in bind.execute(
                sa.select(trades.c.id, trades.c.opened_at, trades.c.legs)
                .where(trades.c.deployment_id == deployment_id, trades.c.opened_at >= earliest)
            ).all():
                new_legs, cash_change, changed = _correct(bind, legs, opened_at, targets)
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
            if isinstance(position, dict) and isinstance(position.get("legs"), dict):
                names = list(position["legs"])
                new_legs, cash_change, changed = _correct(bind, list(position["legs"].values()), position.get("opened_at"), targets)
                if changed:
                    new_state["position"] = {**position, "legs": dict(zip(names, new_legs))}
                    corrections.append(("open position", cash_change, changed))
            holdings = state.get("holdings")
            if isinstance(holdings, dict):
                new_holdings = dict(holdings)
                for key, holding in holdings.items():
                    if isinstance(holding, dict):
                        [fixed], cash_change, changed = _correct(bind, [holding], holding.get("opened_at"), targets)
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
                               "corrected": where, "reason": "entry read a stale price; set to the real 5-minute candle's open"},
                ))


def downgrade() -> None:
    pass  # the wrong prices aren't worth restoring
