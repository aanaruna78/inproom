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

    # Credential vault (INPROOM-108). Base64 of 32 random bytes; old keys "id=base64,id=base64" stay
    # readable so keys can be rotated. Outside env=local a key is mandatory (see app.connectors.vault).
    secrets_key: str | None = None
    secrets_key_id: str = "k1"
    secrets_old_keys: str = ""

    # Outbound Git provider calls (SSRF allowlist). Entries are hostnames (HTTPS only) or explicit origins
    # such as http://localhost:18090 for the local fake provider.
    git_allowed_hosts: str = "api.github.com"
    # Jira sites (INPROOM-130): hostnames, one leading wildcard label, or explicit origins (local fakes).
    jira_allowed_hosts: str = "*.atlassian.net"
    # Confluence sites (INPROOM-134): same rules as the Jira list.
    confluence_allowed_hosts: str = "*.atlassian.net"
    # Where a Confluence attachment download may redirect (INPROOM-166): Atlassian's media service on Cloud.
    # Same entry rules; the site itself is always allowed. Redirected requests never carry the credential.
    confluence_attachment_hosts: str = "api.media.atlassian.com"
    # Embeddings (INPROOM-204, ADR-006). "local": a deterministic hashing embedder (dev, CI, air-gapped;
    # lexical quality only). "openai-compatible": POST {base_url}/embeddings, e.g. Voyage AI, OpenAI, or a
    # self-hosted Ollama/vLLM server. The base URL must pass embedding_allowed_hosts (HTTPS hostnames or
    # explicit origins).
    embedding_provider: str = "local"
    embedding_model: str = "local-hash-v1"
    embedding_dimensions: int = 256
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None  # deployment secret; never logged or returned
    embedding_allowed_hosts: str = "api.voyageai.com, api.openai.com"
    embedding_batch_size: int = 64
    embedding_timeout_seconds: float = 30.0
    embedding_input_type: bool = False  # send input_type document/query (Voyage AI); off for strict APIs
    # Hybrid retrieval (INPROOM-33): candidates per list (keyword, semantic) and the RRF constant.
    retrieval_candidates: int = 50
    retrieval_rrf_k: int = 60
    # Source sync worker (INPROOM-141): incremental reconcile interval, full reconcile interval, job lease.
    sync_interval_seconds: int = 900
    sync_full_interval_seconds: int = 86400
    sync_lease_seconds: int = 300
    # Scheduled connection health checks (INPROOM-145).
    health_interval_seconds: int = 3600
    # Public base URL used in webhook registrations; unset => webhooks are not registered.
    public_url: str | None = None
    # Content store (INPROOM-157): S3-compatible object storage for original source bytes and derivatives.
    # "memory" keeps blobs in-process (unit tests only). Locally this is the SeaweedFS S3 gateway.
    objectstore_backend: str = "s3"
    objectstore_url: str = "http://localhost:18333"
    objectstore_bucket: str = "inproom-content"
    objectstore_region: str = "us-east-1"
    objectstore_access_key: str | None = None
    objectstore_secret_key: str | None = None
    objectstore_timeout_s: int = 5
    content_max_bytes: int = 25 * 1024 * 1024
    # Retention purge per project (INPROOM-160); orphaned blobs are only collected after this grace period.
    content_purge_interval_seconds: int = 86400
    content_orphan_grace_seconds: int = 86400

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
