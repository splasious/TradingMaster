import sqlite3

import pytest

from app.core.config import get_settings
from app.services.backup import service as backup_service


@pytest.fixture
def isolated_backup_env(tmp_path, monkeypatch):
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_path}")

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_service, "BACKUP_DIR", backup_dir)
    return {"db_path": db_path, "backup_dir": backup_dir}


def test_create_backup_produces_a_readable_copy(isolated_backup_env):
    info = backup_service.create_backup()

    assert info.filename.startswith("tradingmaster_")
    assert info.size_bytes > 0

    dest = isolated_backup_env["backup_dir"] / info.filename
    conn = sqlite3.connect(str(dest))
    rows = conn.execute("SELECT v FROM t").fetchall()
    conn.close()
    assert rows == [("hello",)]


def test_list_backups_returns_newest_first(isolated_backup_env):
    first = backup_service.create_backup()
    second = backup_service.create_backup()

    backups = backup_service.list_backups()
    filenames = [b.filename for b in backups]
    assert first.filename in filenames
    assert second.filename in filenames
    assert backups[0].created_at >= backups[-1].created_at


def test_list_backups_empty_when_no_directory(isolated_backup_env):
    assert backup_service.list_backups() == []


def test_resolve_backup_path_rejects_traversal_and_unknown_names(isolated_backup_env):
    backup_service.create_backup()

    assert backup_service.resolve_backup_path("../../etc/passwd") is None
    assert backup_service.resolve_backup_path("not_a_real_backup.db") is None
    assert backup_service.resolve_backup_path("tradingmaster_00000000_000000.db") is None


def test_resolve_backup_path_finds_real_backup(isolated_backup_env):
    info = backup_service.create_backup()
    path = backup_service.resolve_backup_path(info.filename)
    assert path is not None
    assert path.is_file()


def test_create_backup_raises_helpfully_for_postgres(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://user:pass@localhost:5432/tradingmaster")
    monkeypatch.setattr(backup_service, "BACKUP_DIR", tmp_path / "backups")

    with pytest.raises(NotImplementedError, match="pg_dump"):
        backup_service.create_backup()
