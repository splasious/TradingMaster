"""AM OP TRD 15 MIN: close 30 Sep's straddle at 3:00 PM, as its rule says

The deployment was stopped at 11:14:40 IST on 30 Sep with its sideways
straddle (opened 09:52) still open. Stop doesn't close anything, and a
stopped deployment isn't run, so the strategy's own "force-close at 3:00
PM, no exceptions" never happened. Approved the same evening: close it the
way that rule would have -- every leg bought back (or sold) at its real
15:00 price, the open of the 15:00 candle (Kite's final copy first, then the
chart table's) -- recorded as a closed trade with exit reason
time_cutoff_3pm, P&L, P&L % and estimated charges, the cash moved as the
strategy's own close would, and the position cleared. The deployment stays
stopped. Only a stopped AM OP TRD deployment stopped before 15:00 with a
position opened that day is touched; if a leg's 15:00 price isn't on file,
nothing is. A second run finds no position to close.

Revision ID: 3cd892aa3d34
Revises: 4abb8d69a900
Create Date: 2026-09-30 21:00:00.000000

"""
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3cd892aa3d34"
down_revision: Union[str, None] = "4abb8d69a900"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IST = timezone(timedelta(hours=5, minutes=30))
STRATEGY = "AM OP TRD 15 MIN"
SESSION = date(2026, 9, 30)
CUTOFF = datetime(2026, 9, 30, 15, 0, tzinfo=IST)  # the rule's exit time; the 15:00 candle opens then

strategies = sa.table("strategies", sa.column("id", sa.Uuid), sa.column("name", sa.String))
deployments = sa.table(
    "paper_native_deployments", sa.column("id", sa.Uuid), sa.column("strategy_id", sa.Uuid), sa.column("portfolio_id", sa.Uuid),
    sa.column("status", sa.String), sa.column("state", sa.JSON), sa.column("stopped_at", sa.DateTime(timezone=True)),
    sa.column("last_signal", sa.String), sa.column("last_signal_reason", sa.String),
)
trades = sa.table(
    "paper_native_trades", sa.column("id", sa.Uuid), sa.column("deployment_id", sa.Uuid),
    sa.column("opened_at", sa.DateTime(timezone=True)), sa.column("closed_at", sa.DateTime(timezone=True)),
    sa.column("legs", sa.JSON), sa.column("pnl", sa.Float), sa.column("pnl_pct", sa.Float), sa.column("charges", sa.Float),
    sa.column("exit_reason", sa.String),
)
portfolios = sa.table("paper_portfolios", sa.column("id", sa.Uuid), sa.column("user_id", sa.Uuid), sa.column("cash", sa.Float))
instruments = sa.table(
    "instruments", sa.column("id", sa.Uuid), sa.column("symbol", sa.String), sa.column("exchange", sa.String),
    sa.column("instrument_type", sa.String), sa.column("strike", sa.Float), sa.column("option_type", sa.String),
    sa.column("expiry", sa.Date), sa.column("lot_size", sa.Integer), sa.column("underlying_instrument_id", sa.Uuid),
)
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


def _aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _price_at_cutoff(bind, instrument_id: uuid.UUID, symbol: str) -> float | None:
    """The 15:00 candle's open: Kite's final 15m candle (the backfill copy),
    else the chart table's 5m, then 15m."""
    ts = CUTOFF.astimezone(timezone.utc)
    row = bind.execute(
        sa.select(bf_bars.c.open).join(bf_symbols, bf_symbols.c.id == bf_bars.c.symbol_id)
        .where(bf_symbols.c.source == "zerodha_nfo", bf_symbols.c.symbol == symbol, bf_bars.c.timeframe == "15m", bf_bars.c.ts == ts)
    ).first()
    for timeframe in ("5m", "15m"):
        if row is not None:
            break
        row = bind.execute(
            sa.select(candles.c.open).where(candles.c.instrument_id == instrument_id, candles.c.timeframe == timeframe, candles.c.ts == ts)
        ).first()
    return round(float(row[0]), 2) if row is not None else None


def _details(bind, instrument) -> dict:
    """What trade_record.resolve_leg_details adds to a closed leg."""
    from app.services.paper_trading.trade_record import _FNO_UNDERLYING_NAMES

    underlying = None
    if instrument.underlying_instrument_id:
        underlying = bind.execute(sa.select(instruments.c.symbol).where(instruments.c.id == instrument.underlying_instrument_id)).scalar()
    return {
        "instrument_symbol": instrument.symbol, "exchange": instrument.exchange, "instrument_type": instrument.instrument_type,
        "strike": instrument.strike, "option_type": instrument.option_type,
        "expiry": instrument.expiry.isoformat() if instrument.expiry else None, "lot_size": instrument.lot_size,
        "underlying_symbol": _FNO_UNDERLYING_NAMES.get(underlying, underlying) if underlying else None,
    }


def _charges(legs: list[dict], opened_at: datetime, closed_at: datetime) -> float | None:
    try:
        from app.services.paper_trading.trade_record import estimate_charges

        return estimate_charges(legs, opened_at, closed_at)
    except Exception:
        return None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(deployments.c.id, deployments.c.portfolio_id, deployments.c.status, deployments.c.state, deployments.c.stopped_at)
        .join(strategies, strategies.c.id == deployments.c.strategy_id)
        .where(sa.func.trim(strategies.c.name) == STRATEGY)
    ).all()
    for deployment_id, portfolio_id, status, state, stopped_at in rows:
        position = (state or {}).get("position")
        if status != "stopped" or not position or stopped_at is None or _aware(stopped_at) >= CUTOFF:
            continue
        opened_at = _aware(position["opened_at"])
        if opened_at.astimezone(IST).date() != SESSION:
            continue

        legs, cash_change, pnl, notional, closed = [], 0.0, 0.0, 0.0, []
        for leg in position["legs"].values():
            instrument = bind.execute(sa.select(instruments).where(instruments.c.id == uuid.UUID(str(leg["instrument_id"])))).first()
            price = _price_at_cutoff(bind, instrument.id, instrument.symbol) if instrument is not None else None
            if price is None:
                break
            short = leg["side"] == "sell"
            qty, entry = float(leg["quantity"]), float(leg["entry_price"])
            # Buying back a short debits cash, selling a long credits it -- as ctx.close_leg does.
            cash_change += -qty * price if short else qty * price
            pnl += ((entry - price) if short else (price - entry)) * qty
            notional += entry * qty
            legs.append({
                "instrument_id": str(instrument.id), "side": "short" if short else "long", "quantity": qty,
                "entry_price": entry, "exit_price": price, **_details(bind, instrument),
            })
            closed.append((instrument.symbol, "buy" if short else "sell", qty, price))
        else:
            closed_at = CUTOFF.astimezone(timezone.utc)
            user_id = bind.execute(sa.select(portfolios.c.user_id).where(portfolios.c.id == portfolio_id)).scalar()
            bind.execute(trades.insert().values(
                id=uuid.uuid4(), deployment_id=deployment_id, opened_at=opened_at.astimezone(timezone.utc), closed_at=closed_at,
                legs=legs, pnl=pnl, pnl_pct=(pnl / notional * 100) if notional else 0.0,
                charges=_charges(legs, opened_at, closed_at), exit_reason="time_cutoff_3pm",
            ))
            bind.execute(portfolios.update().where(portfolios.c.id == portfolio_id).values(cash=portfolios.c.cash + cash_change))
            bind.execute(deployments.update().where(deployments.c.id == deployment_id).values(
                state={**state, "position": None}, last_signal="COVER",
                last_signal_reason="3:00pm IST cutoff -- closed at the 15:00 prices; the deployment had been stopped at 11:14",
            ))
            for symbol, side, qty, price in closed:
                bind.execute(audit_logs.insert().values(
                    id=uuid.uuid4(), user_id=user_id, action="PAPER_NATIVE_LEG_CLOSED", object_type="paper_native_deployment",
                    object_id=str(deployment_id), previous_value=None,
                    new_value={"instrument": symbol, "side": side, "quantity": qty, "price": price},
                ))
            bind.execute(audit_logs.insert().values(
                id=uuid.uuid4(), user_id=user_id, action="PAPER_NATIVE_POSITION_CLOSED_AT_CUTOFF", object_type="paper_native_deployment",
                object_id=str(deployment_id), previous_value={"position_opened_at": opened_at.isoformat()},
                new_value={"closed_at": CUTOFF.isoformat(), "pnl": round(pnl, 2), "cash_change": round(cash_change, 2),
                           "reason": "stopped at 11:14 with the position open; its 3:00 PM rule never ran (approved 30 Sep)"},
            ))


def downgrade() -> None:
    pass  # a closed paper trade isn't reopened
