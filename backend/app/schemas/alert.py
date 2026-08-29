from datetime import datetime

from pydantic import BaseModel


class AlertOut(BaseModel):
    id: str
    alert_type: str
    severity: str
    title: str
    message: str
    object_type: str | None
    object_id: str | None
    is_read: bool
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread_count: int
