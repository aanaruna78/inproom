"""INPROOM-188 (story INPROOM-31 AC 1, 2): Jira issue security levels - secured issues are withheld by
default, changes at the provider propagate on the next sync, and a policy change applies at once (no
refetch)."""

import copy

import pytest

from app.content.ingest import security_allows
from app.modules.jira_sources import _loosened
from tests import test_jira_sources_integration as helpers
from tests.sync_helpers import due, run_sync
from tests.test_knowledge_feed_integration import feed

pytestmark = pytest.mark.integration
env = helpers.env

LATER = "2026-11-26T10:00:00.000+0000"


def test_the_security_decision():
    assert security_allows(None, None) and security_allows({}, "")  # no level: always served
    assert not security_allows(None, "Staff") and not security_allows({"include_secured": False}, "Staff")
    assert security_allows({"include_secured": True, "allowed_levels": []}, "Board")  # every level
    allow_staff = {"include_secured": True, "allowed_levels": ["staff"]}
    assert security_allows(allow_staff, "Staff") and not security_allows(allow_staff, "Board")
    closed, staff, every = {"include_secured": False}, allow_staff, {"include_secured": True}
    assert _loosened(closed, staff) and _loosened(staff, every) and not _loosened(every, staff)
    assert not _loosened(staff, closed) and _loosened(staff, staff | {"allowed_levels": ["staff", "board"]})


@pytest.fixture
def synced(env):
    e = env
    e.cid = e.connect().json()["id"]
    e.sid = e.select().json()["id"]
    e.sync = lambda: run_sync({"jira": e.jira.transport()}, e.tenant, schedule=True)
    assert e.sync()["result"]["content"]["revisions"] == 5
    return e


def issue(e, key):
    return next(i for i in e.jira.projects["ABC"].issues if i["key"] == key)


def changed_events(e):
    return e.q(
        "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND type='source.jira.security.changed'"
    )


def raw(e, issue_id):
    r = e.client.get(f"/v1/projects/{e.pid}/content/jira/{e.sid}/{issue_id}/revisions/1/raw", headers=e.h())
    return r.status_code, (r.json().get("reason") if r.status_code != 200 else None)


def put_security(e, version, headers=None, **body):
    return e.client.put(
        f"/v1/projects/{e.pid}/sources/jira/{e.sid}/security",
        json={"reason": "board may read staff issues"} | body,
        headers=headers or e.h(**({"If-Match": f'"{version}"'} if version is not None else {})),
    )


def test_security_levels_propagate_from_jira_and_from_the_policy(synced):
    e = synced
    staff = issue(e, "ABC-2")
    staff["fields"] |= {"security": {"id": "10", "name": "Staff"}, "updated": LATER}
    board = copy.deepcopy(issue(e, "ABC-1"))  # a NEW issue created under a level
    board |= {"id": "99001", "key": "ABC-99"}
    board["fields"] |= {"security": {"id": "11", "name": "Board"}, "updated": LATER, "summary": "Board pay"}
    e.jira.projects["ABC"].issues.append(board)
    start = feed(e)["head"]
    due(e.tenant)
    assert e.sync()["result"]["content"]["withheld"] == 2
    assert raw(e, staff["id"]) == (409, "RESTRICTED")
    assert (
        e.q("SELECT count(*) FROM source_objects WHERE tenant_id=:t AND external_id='99001'") == 0
    )  # unstored
    changes = [(x["change"], x["external_id"], x["reason"]) for x in feed(e, start)["items"]]
    assert changes == [("invalidated", staff["id"], "RESTRICTED")]

    source = e.client.get(f"/v1/projects/{e.pid}/sources/jira", headers=e.h()).json()[0]
    assert source["security"] == {"include_secured": False, "allowed_levels": []}
    assert put_security(e, None, include_secured=True).status_code == 428
    assert put_security(e, source["version"] + 5, include_secured=True).status_code == 412
    assert put_security(e, source["version"], include_secured=True, reason="").status_code == 422
    jobs = e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND mode='full' AND status='QUEUED'")
    r = put_security(e, source["version"], include_secured=True, allowed_levels=["staff"])
    assert r.status_code == 200, r.text
    assert r.json()["security"] == {"include_secured": True, "allowed_levels": ["staff"]}
    assert raw(e, staff["id"]) == (200, None)  # restored at once, from the stored ACL
    assert (
        e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND mode='full' AND status='QUEUED'")
        == jobs + 1
    )
    assert changed_events(e) == 1
    again = put_security(e, r.json()["version"], include_secured=True, allowed_levels=["Staff"])
    assert again.json()["version"] == r.json()["version"]  # the same policy: no new version or event
    assert changed_events(e) == 1

    r = put_security(e, r.json()["version"], include_secured=True, allowed_levels=["Staff", "Board"])
    e.sync()  # the queued full sync fetches the issue that was never stored
    assert raw(e, "99001") == (200, None)
    put_security(e, r.json()["version"], include_secured=False)  # closed again: both withheld at once
    assert raw(e, staff["id"]) == (409, "RESTRICTED") and raw(e, "99001") == (409, "RESTRICTED")
