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


def test_sslmode_query_param_is_rewritten_for_asyncpg():
    """Managed Postgres providers (confirmed against InsForge's own
    connection-string) hand back ?sslmode=require -- psycopg's spelling.
    asyncpg's connect() doesn't accept an sslmode kwarg at all and raises
    TypeError outright; it wants ssl=require instead."""
    settings = Settings(database_url="postgres://user:pass@host:5432/db?sslmode=require")
    assert settings.database_url == "postgresql+asyncpg://user:pass@host:5432/db?ssl=require"


def test_url_without_sslmode_is_unaffected():
    settings = Settings(database_url="postgresql+asyncpg://user:pass@host:5432/db")
    assert "ssl" not in settings.database_url
