"""Trading reports (PRD section 54): built from real PaperTrade/LiveTrade
rows, not synthesized. CSV export now; PDF/Excel are a later polish pass
(no new heavy dependency pulled in just for this).
"""

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.models.live_trading import LiveDeployment, LiveTrade
from app.models.paper_trading import PaperDeployment, PaperNativeDeployment, PaperNativeTrade, PaperPortfolio, PaperTrade
from app.models.strategy import Strategy

Environment = Literal["paper", "live"]


@dataclass
class TradeRow:
    environment: str
    strategy_name: str
    instrument_symbol: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str


async def get_trade_rows(
    db: AsyncSession, user_id: uuid.UUID, environment: Environment | None, start: datetime | None, end: datetime | None
) -> list[TradeRow]:
    rows: list[TradeRow] = []

    if environment in (None, "paper"):
        stmt = (
            select(PaperTrade, Strategy.name, Instrument.symbol)
            .join(PaperDeployment, PaperTrade.deployment_id == PaperDeployment.id)
            .join(PaperPortfolio, PaperDeployment.portfolio_id == PaperPortfolio.id)
            .join(Strategy, PaperDeployment.strategy_id == Strategy.id)
            .join(Instrument, PaperDeployment.instrument_id == Instrument.id)
            .where(PaperPortfolio.user_id == user_id)
        )
        if start:
            stmt = stmt.where(PaperTrade.exit_ts >= start)
        if end:
            stmt = stmt.where(PaperTrade.exit_ts <= end)
        for trade, strategy_name, instrument_symbol in (await db.execute(stmt)).all():
            rows.append(TradeRow(
                environment="paper", strategy_name=strategy_name, instrument_symbol=instrument_symbol,
                entry_ts=trade.entry_ts, entry_price=trade.entry_price,
                exit_ts=trade.exit_ts, exit_price=trade.exit_price, quantity=trade.quantity, pnl=trade.pnl,
                pnl_pct=trade.pnl_pct, exit_reason=trade.exit_reason,
            ))

        # Advanced (native) strategy deployments write to their own tables
        # (paper_native_deployments/paper_native_trades -- no instrument_id
        # or fixed quantity to reuse the query above, see
        # PaperNativeDeployment's docstring) but they're still paper-trading
        # activity and belong in the same Reports "paper" bucket. Without
        # this, every closed trade from an Advanced Strategy Deployment was
        # invisible here even though it was already folded into portfolio
        # P&L (see paper_trading.py's _portfolio_out).
        native_stmt = (
            select(PaperNativeTrade, Strategy.name)
            .join(PaperNativeDeployment, PaperNativeTrade.deployment_id == PaperNativeDeployment.id)
            .join(PaperPortfolio, PaperNativeDeployment.portfolio_id == PaperPortfolio.id)
            .join(Strategy, PaperNativeDeployment.strategy_id == Strategy.id)
            .where(PaperPortfolio.user_id == user_id)
        )
        if start:
            native_stmt = native_stmt.where(PaperNativeTrade.closed_at >= start)
        if end:
            native_stmt = native_stmt.where(PaperNativeTrade.closed_at <= end)
        native_trades = (await db.execute(native_stmt)).all()

        instrument_ids: set[uuid.UUID] = set()
        for trade, _name in native_trades:
            for leg in trade.legs or []:
                if leg.get("instrument_id"):
                    instrument_ids.add(uuid.UUID(leg["instrument_id"]))
        instruments: dict[uuid.UUID, Instrument] = {}
        if instrument_ids:
            instruments = {
                i.id: i for i in (await db.execute(select(Instrument).where(Instrument.id.in_(instrument_ids)))).scalars()
            }

        for trade, strategy_name in native_trades:
            legs = trade.legs or []
            symbols = [instruments[uuid.UUID(leg["instrument_id"])].symbol for leg in legs if leg.get("instrument_id") in {str(i) for i in instruments}]
            # Net premium in/out across all legs (short = credit, long =
            # debit) -- the same trade_value/live_value convention
            # paper_native_trading.py's _build_position_out uses for an
            # *open* position, applied here to a *closed* trade's stored
            # entry/exit prices so a multi-leg spread still nets down to one
            # comparable entry/exit figure instead of one row per leg (which
            # would multiply-count a single trade's pnl in win-rate/summary
            # stats below).
            entry_value = sum((leg["entry_price"] if leg["side"] == "short" else -leg["entry_price"]) * leg["quantity"] for leg in legs)
            exit_value = sum((leg["exit_price"] if leg["side"] == "short" else -leg["exit_price"]) * leg["quantity"] for leg in legs)
            rows.append(TradeRow(
                environment="paper", strategy_name=strategy_name, instrument_symbol=", ".join(symbols) or "--",
                entry_ts=trade.opened_at, entry_price=entry_value,
                exit_ts=trade.closed_at, exit_price=exit_value, quantity=1, pnl=trade.pnl,
                pnl_pct=trade.pnl_pct, exit_reason=trade.exit_reason,
            ))

    if environment in (None, "live"):
        stmt = (
            select(LiveTrade, Strategy.name, Instrument.symbol)
            .join(LiveDeployment, LiveTrade.deployment_id == LiveDeployment.id)
            .join(Strategy, LiveDeployment.strategy_id == Strategy.id)
            .join(Instrument, LiveDeployment.instrument_id == Instrument.id)
            .where(LiveDeployment.owner_id == user_id)
        )
        if start:
            stmt = stmt.where(LiveTrade.exit_ts >= start)
        if end:
            stmt = stmt.where(LiveTrade.exit_ts <= end)
        for trade, strategy_name, instrument_symbol in (await db.execute(stmt)).all():
            rows.append(TradeRow(
                environment="live", strategy_name=strategy_name, instrument_symbol=instrument_symbol,
                entry_ts=trade.entry_ts, entry_price=trade.entry_price,
                exit_ts=trade.exit_ts, exit_price=trade.exit_price, quantity=trade.quantity, pnl=trade.pnl,
                pnl_pct=trade.pnl_pct, exit_reason=trade.exit_reason,
            ))

    rows.sort(key=lambda r: r.exit_ts)
    return rows


def rows_to_dicts(rows: list[TradeRow]) -> list[dict]:
    return [
        {
            "environment": r.environment, "strategy_name": r.strategy_name, "instrument_symbol": r.instrument_symbol,
            "entry_ts": r.entry_ts, "entry_price": r.entry_price, "exit_ts": r.exit_ts, "exit_price": r.exit_price,
            "quantity": r.quantity, "pnl": r.pnl, "pnl_pct": r.pnl_pct, "exit_reason": r.exit_reason,
        }
        for r in rows
    ]


def rows_to_csv(rows: list[TradeRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["environment", "strategy", "instrument", "entry_ts", "entry_price", "exit_ts", "exit_price", "quantity", "pnl", "pnl_pct", "exit_reason"])
    for r in rows:
        writer.writerow([r.environment, r.strategy_name, r.instrument_symbol, r.entry_ts.isoformat(), r.entry_price, r.exit_ts.isoformat(), r.exit_price, r.quantity, r.pnl, r.pnl_pct, r.exit_reason])
    return buffer.getvalue()


@dataclass
class ReportSummary:
    trade_count: int
    net_pnl: float
    win_rate_pct: float
    best_trade: float
    worst_trade: float


def summarize(rows: list[TradeRow]) -> ReportSummary:
    if not rows:
        return ReportSummary(trade_count=0, net_pnl=0.0, win_rate_pct=0.0, best_trade=0.0, worst_trade=0.0)
    wins = [r for r in rows if r.pnl > 0]
    return ReportSummary(
        trade_count=len(rows),
        net_pnl=round(sum(r.pnl for r in rows), 2),
        win_rate_pct=round(len(wins) / len(rows) * 100, 2),
        best_trade=round(max(r.pnl for r in rows), 2),
        worst_trade=round(min(r.pnl for r in rows), 2),
    )
