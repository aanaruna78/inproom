"""INPROOM-136 (story INPROOM-24 AC 8, 9): page catalog, page detail, content read-through, sync scope."""

import pytest

from app.modules import confluence_pages
from app.modules.confluence_pages import storage_to_text
from tests import test_confluence_sources_integration as helpers
from tests.fakes import confluence as fc

env = helpers.env
selected = helpers.selected
pytestmark = pytest.mark.integration


def _url(env, path=""):
    return f"/v1/projects/{env.pid}/sources/confluence/{env.sid}{path}"


def test_catalog_pages_with_status_filter_and_cursor(selected, monkeypatch):
    env = selected
    env.select(filters={})  # whole space: 69 current + 2 restricted
    monkeypatch.setattr(confluence_pages, "PAGE_LIMIT", 30)
    ids, cursor = [], None
    while True:
        params = {"status": "CURRENT"} | ({"cursor": cursor} if cursor else {})
        body = env.client.get(_url(env, "/pages"), params=params, headers=env.h()).json()
        ids += [p["page_id"] for p in body["items"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(ids) == 69 and ids == sorted(ids, key=int) and {"103", "104"}.isdisjoint(ids)
    restricted = env.client.get(_url(env, "/pages"), params={"status": "RESTRICTED"}, headers=env.h()).json()
    assert [(p["page_id"], p["restricted"]) for p in restricted["items"]] == [("103", True), ("104", True)]
    bad = env.client.get(_url(env, "/pages"), params={"cursor": "%%%"}, headers=env.h())
    assert bad.status_code == 400


def test_page_detail_has_versions_acl_attachments(selected):
    env = selected
    env.conf.edit(101, "<p>v4</p>")
    env.sync()
    detail = env.client.get(_url(env, "/pages/101"), headers=env.h()).json()
    assert [v["version"] for v in detail["versions"]] == [3, 4] and detail["current_version"] == 4
    assert detail["attachments"][0]["title"] == "diagram.png"
    assert detail["web_url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/101"
    child = env.client.get(_url(env, "/pages/104"), headers=env.h()).json()
    assert child["status"] == "RESTRICTED"
    assert child["restrictions"]["inherited"] == [
        {"page_id": "103", "title": "Leads only", "users": [], "groups": ["bots"]}
    ]


def test_content_is_text_for_current_and_earlier_versions(selected):
    env = selected
    current = env.client.get(_url(env, "/pages/101/content"), headers=env.h()).json()
    assert current["version"] == 3 and current["attachments"][0]["title"] == "diagram.png"
    assert current["text"].splitlines()[:4] == ["# Architecture", "Services:", "- api", "- web"]
    assert (
        "Svc | Owner" in current["text"] and "api | Ada" in current["text"] and "toc" not in current["text"]
    )
    first = env.client.get(_url(env, "/pages/101/content"), params={"version": 1}, headers=env.h()).json()
    assert first["version"] == 1 and first["text"] == "# Architecture\nMonolith first."


def test_content_is_refused_outside_scope(selected):
    env = selected
    restricted = env.client.get(_url(env, "/pages/103/content"), headers=env.h())
    assert restricted.status_code == 409 and restricted.json()["code"] == "PAGE_RESTRICTED"
    env.conf.pages[102].status = "trashed"
    env.sync()
    deleted = env.client.get(_url(env, "/pages/102/content"), headers=env.h())
    assert deleted.status_code == 404 and deleted.json()["code"] == "PAGE_NOT_IN_SCOPE"
    too_new = env.client.get(_url(env, "/pages/101/content"), params={"version": 9}, headers=env.h())
    assert too_new.status_code == 404


def test_no_cross_space_reads(selected):
    env = selected
    hr = env.select("HR", {}).json()["source"]["id"]
    # HR's page 300 is not in ENG's catalog, and ENG's pages are not in HR's.
    assert env.client.get(_url(env, "/pages/300"), headers=env.h()).status_code == 404
    assert env.client.get(_url(env, "/pages/300/content"), headers=env.h()).status_code == 404
    other = f"/v1/projects/{env.pid}/sources/confluence/{hr}/pages/101/content"
    assert env.client.get(other, headers=env.h()).status_code == 404
    # A page moved to another space after the last sync is never served from the old selection.
    env.conf.pages[101].space = "HR"
    moved = env.client.get(_url(env, "/pages/101/content"), headers=env.h())
    assert moved.status_code == 404 and moved.json()["code"] == "PAGE_NOT_IN_SPACE"


def test_scope_lists_active_spaces_and_excludes_paused(selected):
    env = selected
    scope = env.client.get(f"/v1/projects/{env.pid}/sources/scope", headers=env.h()).json()
    assert [(s["space_key"], s["page_count"]) for s in scope["confluence_spaces"]] == [("ENG", 3)]
    assert scope["confluence_spaces"][0]["filters"]["required_labels"] == ["approved"]
    env.conf.revoke_space(fc.VALID_TOKEN, "ENG")
    env.client.post(f"/v1/connections/{env.cid}/check", headers=env.h())
    scope = env.client.get(f"/v1/projects/{env.pid}/sources/scope", headers=env.h()).json()
    assert scope["confluence_spaces"] == []
    assert [(e["kind"], e["full_name"]) for e in scope["excluded"]] == [("confluence", "ENG")]
    paused = env.client.get(_url(env, "/pages/101/content"), headers=env.h())
    assert paused.status_code == 409 and paused.json()["code"] == "SOURCE_PAUSED"


def test_storage_format_conversion():
    storage = (
        "<h2>Steps</h2><ol><li>Build</li><li>Ship</li></ol><p>Run<br/>it</p>"
        '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash</ac:parameter>'
        "<ac:plain-text-body><![CDATA[make up\nmake test]]></ac:plain-text-body></ac:structured-macro>"
        '<p>See <ac:link><ri:page ri:content-title="Other"/></ac:link>&amp; more</p>'
    )
    assert (
        storage_to_text(storage)
        == "## Steps\n- Build\n- Ship\nRun it\n```\nmake up\nmake test\n```\nSee & more"
    )
