"""Database backup (PRD section 53).

SQLite (local dev, the default here): a real file copy, taken through
sqlite3's backup API so it's safe to run against a live database (no
"copying a file mid-write" corruption risk).

PostgreSQL (production): file-copying a live server's data directory isn't
a valid backup strategy, so this deliberately does not attempt one -- it
raises NotImplementedError carrying the pg_dump command an operator should
run instead. Automating a raw pg_dump subprocess from the API process would
mean shelling out with connection credentials, which is worse than just
telling the operator the one command to run.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings

BACKUP_DIR = Path(__file__).resolve().parents[3] / "backups"
_SAFE_NAME = re.compile(r"^tradingmaster_[0-9]{8}_[0-9]{6}\.db$")


@dataclass
class BackupInfo:
    filename: str
    size_bytes: int
    created_at: datetime


def _sqlite_path() -> Path:
    settings = get_settings()
    if not settings.is_sqlite:
        raise NotImplementedError(
            "DATABASE_URL is PostgreSQL. File-copy backup only applies to SQLite. "
            "Back up PostgreSQL with: pg_dump --format=custom --file=backup.dump "
            "<DATABASE_URL>. Restore with: pg_restore --clean --dbname=<DATABASE_URL> backup.dump"
        )
    # sqlite+aiosqlite:///./tradingmaster.db -> ./tradingmaster.db
    raw_path = settings.database_url.split("///", 1)[1]
    return Path(raw_path).resolve()


def create_backup() -> BackupInfo:
    """Copy the live SQLite database via sqlite3's own backup API, which
    takes a consistent snapshot even while the app is writing to it --
    unlike a plain file copy."""
    source_path = _sqlite_path()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"tradingmaster_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
    dest_path = BACKUP_DIR / filename

    source = sqlite3.connect(str(source_path))
    dest = sqlite3.connect(str(dest_path))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    stat = dest_path.stat()
    return BackupInfo(filename=filename, size_bytes=stat.st_size, created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc))


def list_backups() -> list[BackupInfo]:
    if not BACKUP_DIR.exists():
        return []
    backups = []
    for path in BACKUP_DIR.glob("tradingmaster_*.db"):
        stat = path.stat()
        backups.append(BackupInfo(filename=path.name, size_bytes=stat.st_size, created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)))
    return sorted(backups, key=lambda b: b.created_at, reverse=True)


def resolve_backup_path(filename: str) -> Path | None:
    """Only ever returns a path inside BACKUP_DIR matching the exact
    filename format this service generates -- never trusts the caller's
    string as a path fragment (no traversal via '..' or absolute paths)."""
    if not _SAFE_NAME.match(filename):
        return None
    path = BACKUP_DIR / filename
    return path if path.is_file() else None
