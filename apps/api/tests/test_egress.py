"""INPROOM-108: SSRF allowlist for Git provider base URLs."""

import pytest

from app.config import get_settings
from app.connectors.egress import EgressDenied, validate_base_url


@pytest.fixture(autouse=True)
def allowlist(monkeypatch):
    monkeypatch.setenv(
        "INPROOM_GIT_ALLOWED_HOSTS", "api.github.com, github.example.com, http://localhost:18090"
    )
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.github.com", "https://api.github.com"),
        ("https://API.github.com/", "https://api.github.com"),
        ("https://api.github.com:443", "https://api.github.com"),
        ("https://github.example.com/api/v3/", "https://github.example.com/api/v3"),
        ("http://localhost:18090", "http://localhost:18090"),
    ],
)
def test_allowed(url, expected):
    assert validate_base_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://api.github.com",  # plain http
        "https://evil.example.com",  # not allowlisted
        "https://api.github.com.evil.example.com",  # suffix trick
        "https://api.github.com@169.254.169.254",  # userinfo trick
        "https://user:pw@api.github.com",
        "https://169.254.169.254",  # metadata IP
        "https://127.0.0.1",
        "https://api.github.com:8443",  # non-default port
        "http://localhost:18091",  # allowlisted origin is exact
        "https://localhost:18090",
        "https://api.github.com/?next=http://x",
        "file:///etc/passwd",
        "gopher://api.github.com",
        "not a url",
        "",
    ],
)
def test_denied(url):
    with pytest.raises(EgressDenied):
        validate_base_url(url)
