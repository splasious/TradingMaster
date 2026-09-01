from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.reports.service import ReportSummary, get_trade_rows, rows_to_csv, rows_to_dicts, summarize

router = APIRouter()


@router.get("/trades")
async def list_trades(
    environment: Literal["paper", "live"] | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    rows = await get_trade_rows(db, user.id, environment, start, end)
    rows.sort(key=lambda r: r.pnl)
    return rows_to_dicts(rows)


@router.get("/trades.csv")
async def download_trades_csv(
    environment: Literal["paper", "live"] | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    rows = await get_trade_rows(db, user.id, environment, start, end)
    csv_body = rows_to_csv(rows)
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"},
    )


@router.get("/summary", response_model=ReportSummary)
async def trade_summary(
    environment: Literal["paper", "live"] | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReportSummary:
    rows = await get_trade_rows(db, user.id, environment, start, end)
    return summarize(rows)
