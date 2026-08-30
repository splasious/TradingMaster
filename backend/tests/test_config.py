from app.core.config import Settings


def test_plain_postgres_scheme_gets_asyncpg_driver():
    settings = Settings(database_url="postgres://user:pass@host:5432/db")
    assert settings.database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_postgresql_scheme_without_driver_gets_asyncpg_added():
    settings = Settings(database_url="postgresql://user:pass@host:5432/db")
    assert settings.database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_postgresql_scheme_with_asyncpg_driver_is_untouched():
    settings = Settings(database_url="postgresql+asyncpg://user:pass@host:5432/db")
    assert settings.database_url == "postgresql+asyncpg://user:pass@host:5432/db"


def test_sqlite_url_is_untouched():
    settings = Settings(database_url="sqlite+aiosqlite:///./tradingmaster.db")
    assert settings.database_url == "sqlite+aiosqlite:///./tradingmaster.db"
    assert settings.is_sqlite is True
