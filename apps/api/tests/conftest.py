import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import get_engine
from app.main import create_app


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch):
    """Build a TestClient whose app uses the given database URL."""

    def build(database_url: str) -> TestClient:
        monkeypatch.setenv("INPROOM_DATABASE_URL", database_url)
        _reset_caches()
        return TestClient(create_app())

    yield build
    _reset_caches()


@pytest.fixture
def test_database_url() -> str:
    url = os.environ.get("INPROOM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("INPROOM_TEST_DATABASE_URL not set (run via `make test` with the stack up)")
    return url


# A port nothing listens on: used to prove the not-ready path.
UNREACHABLE_DB = "postgresql+psycopg://inproom:inproom@127.0.0.1:1/inproom"
