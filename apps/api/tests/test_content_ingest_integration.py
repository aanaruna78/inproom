"""INPROOM-158 (story INPROOM-27 AC 1, 2, 5, 6): the Confluence and Jira syncs store their items as source
revisions, and the content API serves lineage always and bytes only for in-scope items."""

import hashlib
import json

import pytest

from app.content import ingest
from tests import test_confluence_sources_integration as conf
from tests import test_jira_sources_integration as jira
from tests.fakes import confluence as fc
from tests.sync_helpers import due, run_sync

pytestmark = pytest.mark.integration
env = conf.env
selected = conf.selected
jira_env = jira.env


def content(e, path=""):
    return f"/v1/projects/{e.pid}/content{path}"


def page(e, page_id):
    return content(e, f"/confluence/{e.sid}/{page_id}")


def denials(e, reason):
    return e.q(
        "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='content.read' "
        "AND outcome='DENIED' AND reason=:r",
        r=reason,
    )


def test_confluence_pages_become_revisions_with_exact_bytes_and_lineage(selected):
    e = selected  # selecting the space ran the first sync
    listed = e.client.get(content(e, "?kind=confluence"), headers=e.h()).json()["items"]
    # The restricted pages 103/104 are never stored.
    assert sorted(o["external_id"] for o in listed) == ["100", "101", "102"]
    arch = e.client.get(page(e, "101"), headers=e.h()).json()
    assert (arch["status"], arch["revisions"], arch["current"]["provider_version"]) == ("CURRENT", 1, "3")
    assert arch["acl"]["space_key"] == "ENG" and arch["acl"]["restrictions"]["restricted"] is False
    raw = e.client.get(page(e, "101/revisions/1/raw"), headers=e.h())
    assert raw.status_code == 200 and raw.content == fc.ARCH_V3.encode()
    assert raw.headers["etag"] == f'"{hashlib.sha256(fc.ARCH_V3.encode()).hexdigest()}"'
    assert raw.headers["content-type"].startswith("application/vnd.atlassian.confluence.storage+xml")
    assert (
        e.client.get(
            page(e, "101/revisions/1/raw"), headers=e.h(**{"If-None-Match": raw.headers["etag"]})
        ).status_code
        == 304
    )

    assert e.sync().status_code == 200  # nothing changed: no new revision anywhere
    assert e.q("SELECT count(*) FROM source_revisions WHERE tenant_id=:t") == 3
    e.conf.edit(101, "<h1>Architecture</h1><p>Three services.</p>")
    assert e.sync().status_code == 200
    arch = e.client.get(page(e, "101"), headers=e.h()).json()
    assert [(r["seq"], r["parent_seq"], r["provider_version"]) for r in arch["lineage"]] == [
        (1, None, "3"),
        (2, 1, "4"),
    ]
    assert e.client.get(page(e, "101/revisions/2/raw"), headers=e.h()).text.endswith("Three services.</p>")
    viewer = e.h("vic", roles=())
    assert e.client.get("/v1/me", headers=viewer).status_code == 200
    e.client.put(f"/v1/projects/{e.pid}/members/vic", json={"roles": ["viewer"]}, headers=e.h())
    assert e.client.get(page(e, "101/revisions/1/raw"), headers=viewer).content == fc.ARCH_V3.encode()


def test_out_of_scope_items_keep_lineage_but_their_bytes_are_withheld_and_audited(selected):
    e = selected
    e.conf.pages[102].groups = ["bots"]  # restricted now (still visible to the bot, so not deleted)
    e.conf.pages[100].status = "trashed"  # deleted
    e.conf.pages[101].labels = ["design"]  # no longer matches the space filters
    assert e.sync().status_code == 200
    statuses = {
        o["external_id"]: o["status"] for o in e.client.get(content(e), headers=e.h()).json()["items"]
    }
    assert statuses == {"100": "DELETED", "101": "EXCLUDED", "102": "RESTRICTED"}
    for page_id, reason in (("100", "DELETED"), ("101", "EXCLUDED"), ("102", "RESTRICTED")):
        r = e.client.get(page(e, f"{page_id}/revisions/1/raw"), headers=e.h())
        assert r.status_code == 409 and r.json()["code"] == "CONTENT_WITHHELD", r.text
        assert r.json()["reason"] == reason and denials(e, reason) == 1
        assert e.client.get(page(e, page_id), headers=e.h()).json()["lineage"][0]["stored"] is True
    # Back in scope with the same bytes: no new revision, the bytes are served again.
    e.conf.pages[101].labels = ["approved", "design"]
    assert e.sync().status_code == 200
    assert e.client.get(page(e, "101"), headers=e.h()).json()["revisions"] == 1
    assert e.client.get(page(e, "101/revisions/1/raw"), headers=e.h()).status_code == 200
    # A paused (excluded) source withholds everything it holds.
    assert e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/exclude", headers=e.h()).is_success
    r = e.client.get(page(e, "101/revisions/1/raw"), headers=e.h())
    assert r.status_code == 409 and r.json()["reason"] == "SOURCE_PAUSED"


def test_unknown_items_and_revisions_are_not_found(selected):
    e = selected
    assert e.client.get(page(e, "999"), headers=e.h()).json()["code"] == "CONTENT_NOT_FOUND"
    assert e.client.get(page(e, "101/revisions/7/raw"), headers=e.h()).json()["code"] == "REVISION_NOT_FOUND"
    assert e.client.get(content(e, "?cursor=bm9wZQ"), headers=e.h()).json()["code"] == "INVALID_CURSOR"
    first = e.client.get(content(e, "?limit=2"), headers=e.h()).json()
    rest = e.client.get(content(e, f"?limit=2&cursor={first['next_cursor']}"), headers=e.h()).json()
    assert [o["external_id"] for o in first["items"] + rest["items"]] == ["100", "101", "102"]
    assert rest["next_cursor"] is None


def test_jira_issues_are_stored_as_canonical_records(jira_env):
    e = jira_env
    e.cid = e.connect().json()["id"]
    e.sid = e.select().json()["id"]
    assert run_sync({"jira": e.jira.transport()}, e.tenant)["status"] == "SUCCEEDED"
    items = e.client.get(content(e, "?kind=jira"), headers=e.h()).json()["items"]
    assert len(items) == 5 and {i["status"] for i in items} == {"CURRENT"}
    issue = next(i for i in items if i["title"].startswith("ABC-1:"))
    assert issue["acl"] == {"jira_project_id": "10100", "jira_key": "ABC", "security_level": None}
    raw = e.client.get(content(e, f"/jira/{e.sid}/{issue['external_id']}/revisions/1/raw"), headers=e.h())
    record = json.loads(raw.content)
    assert (record["key"], raw.headers["content-type"]) == ("ABC-1", ingest.JIRA_MEDIA)
    assert record["fields"]["creator"] == {"accountId": "acc-ada", "displayName": "Ada"}
    assert raw.content == ingest.jira_record(
        {"id": record["id"], "key": "ABC-1", "fields": record["fields"]}, list(record["fields"])
    )  # canonical: re-encoding the stored record reproduces it byte for byte
    due(e.tenant)
    assert run_sync({"jira": e.jira.transport()}, e.tenant)["result"]["content"]["revisions"] == 0


def test_canonical_jira_record_ignores_volatile_and_linked_issue_details():
    fields = ingest.jira_fields({"fields": {"acceptance_criteria": {"field_id": "customfield_1"}}})
    assert fields[-1] == "customfield_1" and "updated" in fields

    def issue(**overrides):
        base = {
            "summary": "S",
            "assignee": {"accountId": "a1", "displayName": "Ann", "avatarUrls": {"48x48": "https://x/1"}},
            "issuelinks": [
                {
                    "id": "9",
                    "type": {"name": "Blocks", "self": "https://x"},
                    "outwardIssue": {"id": "2", "key": "ABC-2", "fields": {"status": {"name": "To Do"}}},
                }
            ],
            "parent": {"id": "1", "key": "ABC-1", "fields": {"summary": "Epic"}},
            "watches": {"watchCount": 1},
        }
        return {"id": "3", "key": "ABC-3", "fields": base | overrides}

    a = ingest.jira_record(issue(), fields)
    b = issue(watches={"watchCount": 7})
    b["fields"]["assignee"]["avatarUrls"] = {"48x48": "https://x/2"}
    b["fields"]["issuelinks"][0]["outwardIssue"]["fields"]["status"]["name"] = "Done"
    b["fields"]["parent"]["fields"]["summary"] = "Renamed epic"
    assert ingest.jira_record(b, fields) == a
    assert ingest.jira_record(issue(summary="S2"), fields) != a
    assert b"avatarUrls" not in a and b"watches" not in a
