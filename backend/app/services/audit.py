import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


async def write_audit_log(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    previous_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=object_id,
            previous_value=previous_value,
            new_value=new_value,
            ip_address=ip_address,
        )
    )
    await db.flush()
