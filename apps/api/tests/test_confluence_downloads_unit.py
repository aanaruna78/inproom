"""INPROOM-166: attachment downloads use a fixed path, a byte cap, and only allowlisted redirect hosts."""

import httpx
import pytest

from app.connectors.base import ProviderError
from app.connectors.confluence import ConfluenceProvider
from app.connectors.http import DownloadTooLarge
from app.connectors.vault import Secret


def provider(handler):
    return ConfluenceProvider(
        "https://acme.atlassian.net", Secret("t"), email="a@b.c", transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://acme.atlassian.net/wiki/download/attachments/1/x.pdf", True),
        ("https://api.media.atlassian.com/file/abc/binary?token=t", True),
        ("http://api.media.atlassian.com/file/abc/binary", False),
        ("https://api.media.atlassian.com:8443/file", False),
        ("https://evil.example/file", False),
        ("https://169.254.169.254/latest/meta-data", False),
        ("https://user:pw@api.media.atlassian.com/file", False),
        ("https://other.atlassian.net/wiki/download/x", False),
    ],
)
def test_redirect_allowlist(url, allowed):
    assert provider(lambda r: httpx.Response(200))._redirect_allowed(url) is allowed


def test_download_is_capped_and_ids_are_validated():
    p = provider(lambda r: httpx.Response(200, content=b"x" * 100))
    assert p.download_attachment("1", "att1", 100) == b"x" * 100
    with pytest.raises(DownloadTooLarge):
        p.download_attachment("1", "att1", 99)
    with pytest.raises(ProviderError):
        p.download_attachment("1", "../../admin", 99)


def test_a_redirect_is_followed_once_without_the_credential():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "acme.atlassian.net":
            return httpx.Response(302, headers={"location": "https://api.media.atlassian.com/file/a/binary"})
        return httpx.Response(302, headers={"location": "https://api.media.atlassian.com/file/b/binary"})

    with pytest.raises(ProviderError, match="more than once"):
        provider(handler).download_attachment("1", "att1", 10)
    assert seen[0][1] is not None and seen[1] == ("api.media.atlassian.com", None)
