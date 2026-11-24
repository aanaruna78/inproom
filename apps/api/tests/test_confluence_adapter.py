"""INPROOM-134/138: Confluence adapter against the fake - Cloud REST v2 + DC v1: spaces, cursor paging,
pages, versions, ACLs, errors."""

import pytest

from app.config import get_settings
from app.connectors.base import (
    CredentialInvalid,
    NotFoundOrNoAccess,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from app.connectors.confluence import ConfluenceProvider, content_hash
from app.connectors.egress import EgressDenied
from app.connectors.vault import Secret
from tests.fakes import confluence as fc

SITE = "https://acme.atlassian.net"


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("INPROOM_CONFLUENCE_ALLOWED_HOSTS", "*.atlassian.net, wiki.corp.example.com")
    get_settings.cache_clear()
    return fc.standard()


def provider(fake, token=fc.VALID_TOKEN, email=fc.EMAIL, site=SITE):
    return ConfluenceProvider(
        site, Secret(token), email=email, transport=fake.transport(), sleep=lambda s: None
    )


def test_whoami_spaces_and_write_capability(fake):
    p = provider(fake)
    assert p.whoami().login == fc.EMAIL
    spaces, nxt = p.list_spaces()
    assert [s.key for s in spaces] == ["ENG", "HR"] and nxt is None  # SECRET is never listed
    assert [s.key for s in p.list_spaces(q="people")[0]] == ["HR"]
    assert p.get_space("ENG").write_capable is True
    assert provider(fake, fc.READONLY_TOKEN, "reader@acme.example").get_space("ENG").write_capable is False
    with pytest.raises(NotFoundOrNoAccess):
        p.get_space("SECRET")


def test_pages_follow_next_links_and_carry_metadata(fake):
    listing = provider(fake).iter_pages(provider(fake).get_space("ENG"))
    pages = {pg.id: pg for pg in listing.pages}
    assert (
        not listing.truncated and len(pages) == 71
    )  # 6 visible named pages + 65 notes, over 2 provider pages
    assert {"105", "106", "110"}.isdisjoint(pages)  # draft, trashed, restricted to someone else
    arch = pages["101"]
    assert (arch.version, arch.author, arch.message, arch.parent_id, arch.ancestors) == (
        3,
        "Ada",
        "detail",
        "100",
        ["100"],
    )
    assert arch.labels == ["approved", "design"] and arch.content_sha256 == content_hash(fc.ARCH_V3)
    assert [(a.title, a.media_type, a.size) for a in arch.attachments] == [("diagram.png", "image/png", 2048)]
    assert pages["103"].restrictions == {"users": [], "groups": ["bots"]}
    assert pages["104"].ancestors == ["100", "103"] and pages["104"].restrictions == {
        "users": [],
        "groups": [],
    }
    capped = provider(fake).iter_pages(provider(fake).get_space("ENG"), cap=10)
    assert capped.truncated and len(capped.pages) == 10


def test_bodies_versions_and_data_center(fake):
    p = provider(fake)
    assert p.get_page("101").version == 3 and p.get_page("101", version=1).storage == fc.ARCH_V1
    assert [v.number for v in p.page_versions("101")] == [1, 2, 3]
    dc = ConfluenceProvider("https://wiki.corp.example.com", Secret(fc.DC_PAT), transport=fake.transport())
    assert dc.whoami().login == "dc-bot" and dc.get_page("101").space_id == "1"
    dc_pages = dc.iter_pages(dc.get_space("ENG")).pages
    assert len(dc_pages) == 71 and next(pg for pg in dc_pages if pg.id == "104").ancestors == ["100", "103"]
    assert all(path.startswith("/rest/api") for _, path in fake.calls[-4:])


def test_errors_retries_and_redirects(fake):
    with pytest.raises(CredentialInvalid):
        provider(fake, fc.BAD_TOKEN).whoami()
    with pytest.raises(NotFoundOrNoAccess):
        provider(fake).get_page("400")  # SECRET space
    fake.failures = ["timeout", "500"]
    assert provider(fake).whoami().login == fc.EMAIL
    fake.failures = ["500"] * 3
    with pytest.raises(ProviderUnavailable, match="Confluence did not respond successfully after 3 attempts"):
        provider(fake).whoami()
    fake.failures = ["ratelimit"]
    with pytest.raises(RateLimited) as exc:
        provider(fake).whoami()
    assert exc.value.retry_after == 42
    fake.failures = ["redirect"]
    with pytest.raises(ProviderError, match="redirect"):
        provider(fake).whoami()


def test_cloud_uses_rest_v2_with_cursor_paging(fake):
    """INPROOM-138: Cloud spaces/pages/versions/labels/attachments go to /wiki/api/v2 and follow cursors.
    Only the current user and read restrictions stay on v1 (v2 has no equivalent); the fake answers 410 to
    any other /wiki/rest/api call, so a regression to v1 cannot pass."""
    p = provider(fake)
    p.whoami()
    space = p.get_space("ENG")
    p.list_spaces()
    listing = p.iter_pages(space)
    p.get_page("101", version=1)
    p.page_versions("101")
    v1 = {path for _, path in fake.calls if path.startswith("/wiki/rest/api")}
    assert v1 == {"/wiki/rest/api/user/current"} | {
        f"/wiki/rest/api/content/{pg.id}/restriction/byOperation/read" for pg in listing.pages
    }
    listing_calls = [path for _, path in fake.calls if path == "/wiki/api/v2/spaces/1/pages"]
    assert len(listing_calls) == 2 and len(listing.pages) == 71  # two cursor pages


def test_next_links_cannot_steer_requests_elsewhere(fake, monkeypatch):
    """A hostile `_links.next` pointing at another host/path is reduced to its query string."""
    real = fake._cursored

    def hostile(results, params, path):
        out = real(results, params, path)
        nxt = out["_links"].get("next")
        if nxt:
            out["_links"]["next"] = "http://169.254.169.254/latest" + nxt[nxt.index("?") :]
        return out

    monkeypatch.setattr(fake, "_cursored", hostile)
    p = provider(fake)
    assert len(p.iter_pages(p.get_space("ENG")).pages) == 71
    assert all(path.startswith(("/wiki/api/v2/", "/wiki/rest/api/")) for _, path in fake.calls)


def test_site_outside_allowlist_is_refused_before_any_call(fake):
    for site in ("https://169.254.169.254", "https://atlassian.net.evil.com", "http://acme.atlassian.net"):
        with pytest.raises(EgressDenied):
            provider(fake, site=site)
    assert fake.calls == []
