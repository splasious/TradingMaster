"""Alert generation (PRD sections 38, 52). Alerts are user-facing
notifications ("your stop-loss fired"), distinct from the audit log
(compliance record of who-did-what -- app/services/audit.py). Both often
fire from the same event, for different audiences.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert, AlertSeverity


async def create_alert(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    alert_type: str,
    severity: AlertSeverity,
    title: str,
    message: str,
    object_type: str | None = None,
    object_id: str | None = None,
) -> Alert:
    alert = Alert(
        user_id=user_id, alert_type=alert_type, severity=severity.value, title=title, message=message,
        object_type=object_type, object_id=object_id,
    )
    db.add(alert)
    await db.flush()
    return alert
