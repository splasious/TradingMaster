import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.alert import Alert
from app.models.user import User
from app.schemas.alert import AlertOut, UnreadCountOut

router = APIRouter()


def _out(alert: Alert) -> AlertOut:
    return AlertOut(
        id=str(alert.id), alert_type=alert.alert_type, severity=alert.severity, title=alert.title, message=alert.message,
        object_type=alert.object_type, object_id=alert.object_id, is_read=alert.is_read, created_at=alert.created_at,
    )


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    unread_only: bool = Query(False),
    severity: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AlertOut]:
    stmt = select(Alert).where(Alert.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Alert.is_read.is_(False))
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    result = await db.execute(stmt.order_by(Alert.created_at.desc()).limit(200))
    return [_out(a) for a in result.scalars().all()]


@router.get("/unread-count", response_model=UnreadCountOut)
async def unread_count(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> UnreadCountOut:
    result = await db.execute(select(func.count()).select_from(Alert).where(Alert.user_id == user.id, Alert.is_read.is_(False)))
    return UnreadCountOut(unread_count=result.scalar_one())


@router.post("/{alert_id}/read", response_model=AlertOut)
async def mark_read(alert_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> AlertOut:
    alert = await db.get(Alert, uuid.UUID(alert_id))
    if alert is None or alert.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    alert.is_read = True
    await db.commit()
    await db.refresh(alert)
    return _out(alert)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    await db.execute(update(Alert).where(Alert.user_id == user.id, Alert.is_read.is_(False)).values(is_read=True))
    await db.commit()
