import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from app.auth import get_jwks_cache
from app.config import get_settings
from app.db import get_engine
from app.main import create_app
from tests.auth_helpers import TokenFactory

# A port nothing listens on: used to prove the not-ready path.
UNREACHABLE_DB = "postgresql+psycopg://inproom:inproom@127.0.0.1:1/inproom"


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_jwks_cache.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_settings():
    _reset_caches()
    yield
    _reset_caches()


@pytest.fixture
def tokens() -> TokenFactory:
    return TokenFactory()


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch, tokens: TokenFactory):
    """Build a TestClient whose app uses the given database URL and the test JWKS."""

    def build(database_url: str, app: FastAPI | None = None) -> TestClient:
        monkeypatch.setenv("INPROOM_DATABASE_URL", database_url)
        _reset_caches()
        app = app or create_app()
        cache = tokens.cache()
        app.dependency_overrides[get_jwks_cache] = lambda: cache
        return TestClient(app)

    return build


@pytest.fixture
def test_database_url() -> str:
    url = os.environ.get("INPROOM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("INPROOM_TEST_DATABASE_URL not set (run via `make test` with the stack up)")
    return url


@pytest.fixture
def db_client(client_for, test_database_url) -> TestClient:
    return client_for(test_database_url)


@pytest.fixture
def owner_engine(test_database_url, monkeypatch) -> Engine:
    """Connection as the migration owner (superuser locally) - bypasses RLS; for seeding and inspection."""
    monkeypatch.setenv("INPROOM_DATABASE_URL", test_database_url)
    engine = create_engine(test_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def make_tenant(owner_engine):
    """Insert a tenant row (as owner) and return its id. Each test gets fresh random tenants."""

    def make(name: str = "tenant") -> uuid.UUID:
        tid = uuid.uuid4()
        with owner_engine.begin() as conn:
            conn.execute(text("INSERT INTO tenants (id, name) VALUES (:id, :n)"), {"id": tid, "n": name})
        return tid

    return make


@pytest.fixture
def fake_github(db_client):
    """The standard fake GitHub (tests/fakes/github.py) wired into db_client's app instead of the network."""
    from app.connectors.registry import get_git_transport
    from tests.fakes.github import standard

    fake = standard()
    db_client.app.dependency_overrides[get_git_transport] = fake.transport
    return fake


@pytest.fixture
def fake_jira(db_client, monkeypatch):
    """The standard fake Jira (tests/fakes/jira.py) wired into db_client's app instead of the network."""
    from app.config import get_settings
    from app.connectors.registry import get_jira_transport
    from tests.fakes.jira import standard

    monkeypatch.setenv("INPROOM_JIRA_ALLOWED_HOSTS", "*.atlassian.net, jira.corp.example.com")
    get_settings.cache_clear()
    fake = standard()
    db_client.app.dependency_overrides[get_jira_transport] = fake.transport
    return fake


@pytest.fixture
def fake_confluence(db_client, monkeypatch):
    """The standard fake Confluence (tests/fakes/confluence.py) wired into db_client's app."""
    from app.config import get_settings
    from app.connectors.registry import get_confluence_transport
    from tests.fakes.confluence import standard

    monkeypatch.setenv("INPROOM_CONFLUENCE_ALLOWED_HOSTS", "*.atlassian.net, wiki.corp.example.com")
    get_settings.cache_clear()
    fake = standard()
    db_client.app.dependency_overrides[get_confluence_transport] = fake.transport
    return fake


@pytest.fixture
def no_content_jobs(monkeypatch):
    """Turn off the content pipeline's follow-up jobs: derivative producers (INPROOM-159) and the retention
    purge schedule (INPROOM-160). For suites about the source sync queue itself, whose job counts and
    listings predate the content pipeline (which has its own suites)."""
    from app.content import producers, retention

    monkeypatch.setattr(producers, "PRODUCERS", {})
    monkeypatch.setattr(retention, "AUTO_SCHEDULE", False)
