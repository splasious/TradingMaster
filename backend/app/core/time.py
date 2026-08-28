from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware_utc(value: datetime) -> datetime:
    """SQLite doesn't preserve tzinfo on DateTime(timezone=True) columns, so a
    value round-tripped through it comes back naive even though it was always
    UTC. Postgres preserves it correctly. Normalize here so comparisons work
    the same on both backends."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
