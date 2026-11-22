from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden with an INPROOM_* environment variable."""

    model_config = SettingsConfigDict(env_prefix="INPROOM_", extra="ignore")

    env: str = "local"
    version: str = "0.1.0"
    database_url: str = "postgresql+psycopg://inproom:inproom@localhost:55432/inproom"
    db_connect_timeout_s: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
