"""QA probes for INPROOM-27 (build/qa/INPROOM-27/test_probes.py at 90c276e), committed as regressions.

Independent content-store checks on PostgreSQL, SeaweedFS and fake Confluence. Unfixed cases are strict xfails
naming their defect:
- INPROOM-161: content restored after a retention purge must be readable again;
- INPROOM-162: the daily purge must be scheduled even when no source is syncing any more;
- INPROOM-163: an ACL-only change (same body) must refresh the stored object's ACL.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.content import retention
from app.content import store as blobs
from app.sync import worker
from tests import test_confluence_sources_integration as h

env, selected = h.env, h.selected
pytestmark = pytest.mark.integration


def detail(e, item="101"):
    return e.client.get(f"/v1/projects/{e.pid}/content/confluence/{e.sid}/{item}", headers=e.h())


def raw(e, item="101", seq=1):
    return e.client.get(
        f"/v1/projects/{e.pid}/content/confluence/{e.sid}/{item}/revisions/{seq}/raw", headers=e.h()
    )


def sql(engine, query, **values):
    with engine.begin() as conn:
        return conn.execute(text(query), values)


@pytest.mark.xfail(strict=True, reason="INPROOM-161: a purged current revision stays current after restore")
def test_restored_page_after_retention_has_readable_current_revision(selected, owner_engine):
    e = selected
    original = raw(e).content
    e.conf.pages[101].status = "trashed"
    assert e.sync().status_code == 200
    sql(
        owner_engine,
        "UPDATE source_objects SET deleted_at=now()-interval '31 days' "
        "WHERE tenant_id=:t AND external_id='101'",
        t=e.tenant,
    )
    result = retention.purge_project(e.tenant, uuid.UUID(e.pid), blobs.get_store(), datetime.now(UTC))
    assert result["revisions_purged"] == 1
    assert raw(e).status_code == 409
    e.conf.pages[101].status = "current"
    assert e.sync().status_code == 200
    obj = detail(e).json()
    assert obj["status"] == "CURRENT"
    read = raw(e, seq=obj["current"]["seq"])
    assert (
        read.status_code == 200 and read.content == original
    ), f"restored CURRENT page still unreadable: HTTP {read.status_code}, {read.text}"


@pytest.mark.xfail(strict=True, reason="INPROOM-162: purge is only scheduled by content ingestion")
def test_purge_due_after_source_removed_is_scheduled(selected, owner_engine):
    e = selected
    while worker.run_once("qa", transports={}, schedule=False, tenant=e.tenant):
        pass
    assert (
        e.client.delete(f"/v1/projects/{e.pid}/sources/confluence/{e.cid}:ENG", headers=e.h()).status_code
        == 204
    )
    sql(
        owner_engine,
        "UPDATE source_sync_state SET next_due_at=now()-interval '1 day' "
        "WHERE tenant_id=:t AND source_kind='purge'",
        t=e.tenant,
    )
    worker.schedule_tick(e.tenant)
    queued = e.q(
        "SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND source_kind='purge' AND status='QUEUED'"
    )
    assert queued == 1, f"daily purge never re-enqueued after last source removal: {queued} queued"


@pytest.mark.xfail(strict=True, reason="INPROOM-163: unchanged bodies skip the object ACL update")
def test_acl_changes_without_body_changes_refresh_stored_acl(selected):
    e = selected
    assert e.select(filters={"required_labels": ["approved"], "include_restricted": True}).status_code == 200
    assert detail(e).json()["acl"]["restrictions"]["restricted"] is False
    e.conf.pages[101].groups = ["bots"]
    assert e.sync().status_code == 200
    obj = detail(e).json()
    assert obj["status"] == "CURRENT"
    assert (
        obj["acl"]["restrictions"]["restricted"] is True
    ), f"synced object still advertises unrestricted ACL: {obj['acl']}"


def test_repeated_sync_preserves_exact_current_content(selected):
    e = selected
    before = raw(e)
    assert before.status_code == 200
    assert e.sync().status_code == 200
    after = raw(e)
    assert after.status_code == 200 and after.content == before.content
    assert detail(e).json()["revisions"] == 1


def test_exclusion_keeps_metadata_and_denies_raw(selected):
    e = selected
    assert (
        e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/exclude", headers=e.h()).status_code
        == 200
    )
    assert detail(e).status_code == 200
    response = raw(e)
    assert response.status_code == 409 and response.json()["code"] == "CONTENT_WITHHELD"
