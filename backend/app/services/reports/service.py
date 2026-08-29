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

from app.models.live_trading import LiveDeployment, LiveTrade
from app.models.paper_trading import PaperDeployment, PaperPortfolio, PaperTrade
from app.models.strategy import Strategy

Environment = Literal["paper", "live"]


@dataclass
class TradeRow:
    environment: str
    strategy_name: str
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
            select(PaperTrade, Strategy.name)
            .join(PaperDeployment, PaperTrade.deployment_id == PaperDeployment.id)
            .join(PaperPortfolio, PaperDeployment.portfolio_id == PaperPortfolio.id)
            .join(Strategy, PaperDeployment.strategy_id == Strategy.id)
            .where(PaperPortfolio.user_id == user_id)
        )
        if start:
            stmt = stmt.where(PaperTrade.exit_ts >= start)
        if end:
            stmt = stmt.where(PaperTrade.exit_ts <= end)
        for trade, strategy_name in (await db.execute(stmt)).all():
            rows.append(TradeRow(
                environment="paper", strategy_name=strategy_name, entry_ts=trade.entry_ts, entry_price=trade.entry_price,
                exit_ts=trade.exit_ts, exit_price=trade.exit_price, quantity=trade.quantity, pnl=trade.pnl,
                pnl_pct=trade.pnl_pct, exit_reason=trade.exit_reason,
            ))

    if environment in (None, "live"):
        stmt = (
            select(LiveTrade, Strategy.name)
            .join(LiveDeployment, LiveTrade.deployment_id == LiveDeployment.id)
            .join(Strategy, LiveDeployment.strategy_id == Strategy.id)
            .where(LiveDeployment.owner_id == user_id)
        )
        if start:
            stmt = stmt.where(LiveTrade.exit_ts >= start)
        if end:
            stmt = stmt.where(LiveTrade.exit_ts <= end)
        for trade, strategy_name in (await db.execute(stmt)).all():
            rows.append(TradeRow(
                environment="live", strategy_name=strategy_name, entry_ts=trade.entry_ts, entry_price=trade.entry_price,
                exit_ts=trade.exit_ts, exit_price=trade.exit_price, quantity=trade.quantity, pnl=trade.pnl,
                pnl_pct=trade.pnl_pct, exit_reason=trade.exit_reason,
            ))

    rows.sort(key=lambda r: r.exit_ts)
    return rows


def rows_to_csv(rows: list[TradeRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["environment", "strategy", "entry_ts", "entry_price", "exit_ts", "exit_price", "quantity", "pnl", "pnl_pct", "exit_reason"])
    for r in rows:
        writer.writerow([r.environment, r.strategy_name, r.entry_ts.isoformat(), r.entry_price, r.exit_ts.isoformat(), r.exit_price, r.quantity, r.pnl, r.pnl_pct, r.exit_reason])
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
