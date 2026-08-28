from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "TradingMaster"
    environment: str = "development"

    # database_url examples:
    #   postgresql+asyncpg://user:pass@localhost:5432/tradingmaster   (production/docker)
    #   sqlite+aiosqlite:///./tradingmaster.db                        (local dev without docker)
    database_url: str = "sqlite+aiosqlite:///./tradingmaster.db"

    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Fernet key for encrypting broker credentials at rest.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    credential_encryption_key: str = "hkGwj1XN4gz1Fh0S0e8OqjF5m9jK3vY0Wc7hZzYw6yQ="

    cors_origins: list[str] = ["http://localhost:3000"]

    seed_admin_email: str = "admin@tradingmaster.internal"
    seed_admin_password: str = "ChangeMe123!"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
