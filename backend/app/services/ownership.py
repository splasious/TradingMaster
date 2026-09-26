"""Each user's strategies -- and everything run from them: backtests,
optimizations, paper and live deployments -- are private to that user.
Administrators follow the same rule; their role adds system controls
(users, kill switch, data backfill), not access to other users' work."""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy import Strategy
from app.models.user import User


async def owns_strategy(db: AsyncSession, strategy_id: uuid.UUID, user: User) -> bool:
    owner_id = (await db.execute(select(Strategy.owner_id).where(Strategy.id == strategy_id))).scalar_one_or_none()
    return owner_id == user.id


async def require_strategy_owner(db: AsyncSession, strategy_id: str, user: User) -> None:
    """404 -- not 403 -- for someone else's strategy, so its existence isn't revealed."""
    if not await owns_strategy(db, uuid.UUID(strategy_id), user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")


async def owned_job(db: AsyncSession, model, job_id: str, user: User, not_found: str):
    """A backtest/optimization job, if it was run from one of the user's own strategies."""
    job = await db.get(model, uuid.UUID(job_id))
    if job is None or not await owns_strategy(db, job.strategy_id, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found)
    return job
