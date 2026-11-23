from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value can be overridden with an INPROOM_* environment variable."""

    model_config = SettingsConfigDict(env_prefix="INPROOM_", extra="ignore")

    env: str = "local"
    version: str = "0.1.0"
    database_url: str = "postgresql+psycopg://inproom:inproom@localhost:55432/inproom"
    db_connect_timeout_s: int = 2
    workspace_project_limit: int = 50

    # OIDC resource-server settings (defaults target the local Keycloak realm).
    oidc_issuer: str = "http://localhost:18080/realms/inproom"
    oidc_audience: str = "inproom-api"
    oidc_jwks_url: str | None = None
    oidc_leeway_s: int = 30
    oidc_jwks_timeout_s: int = 3

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
