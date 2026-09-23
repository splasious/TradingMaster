"""Alert generation (PRD sections 38, 52). Alerts are user-facing
notifications ("your stop-loss fired"), distinct from the audit log
(compliance record of who-did-what -- app/services/audit.py). Both often
fire from the same event, for different audiences.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert, AlertSeverity

_TITLE_MAX = Alert.__table__.c.title.type.length
_MESSAGE_MAX = Alert.__table__.c.message.type.length


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
    """Title/message are cut to their column lengths here rather than left
    to the database: Postgres rejects an over-long varchar outright, and
    since this flushes immediately, that error would abort the caller's
    whole transaction -- e.g. a native strategy's tick, rolling back
    everything it did that tick, then failing identically on every retry.
    Callers that also push the text elsewhere (Telegram) keep the full
    version."""
    alert = Alert(
        user_id=user_id, alert_type=alert_type, severity=severity.value,
        title=_fit(title, _TITLE_MAX), message=_fit(message, _MESSAGE_MAX),
        object_type=object_type, object_id=object_id,
    )
    db.add(alert)
    await db.flush()
    return alert
