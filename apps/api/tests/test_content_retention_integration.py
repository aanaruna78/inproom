"""INPROOM-160 (story INPROOM-27 AC 7, 8): retention purge keeps lineage and shared blobs; object store
outages and a crash between the blob write and the commit recover to an exact state."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.content import retention
from app.content import store as blobs
from app.content.revisions import record_revision
from app.db import get_engine, tenant_session
from app.modules import confluence_sources
from app.sync import worker
from tests import test_confluence_sources_integration as conf
from tests.sync_helpers import due, run_sync

pytestmark = pytest.mark.integration
env = conf.env
selected = conf.selected
RUNBOOKS = "<p>Restart the api.</p>"  # page 102's body


def page(e, page_id, path=""):
    return e.client.get(f"/v1/projects/{e.pid}/content/confluence/{e.sid}/{page_id}{path}", headers=e.h())


def set_retention(e, **policy):
    current = e.client.get(f"/v1/projects/{e.pid}/settings", headers=e.h()).json()
    body = {"settings": {"sources": current["settings"]["sources"] | {"retention": policy}}}
    r = e.client.put(
        f"/v1/projects/{e.pid}/settings", json=body, headers=e.h(**{"If-Match": f'"{current["version"]}"'})
    )
    assert r.status_code == 200, r.text


def owner_sql(owner_engine, sql, **params):
    with owner_engine.begin() as conn:
        conn.execute(text(sql), params)


def sync_worker(e):
    r = e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/reconcile", headers=e.h())
    assert r.status_code == 202, r.text
    return run_sync({"confluence": e.conf.transport()}, e.tenant, schedule=False)


def test_purge_drops_old_bytes_keeps_lineage_and_shared_blobs(selected, owner_engine):
    e = selected
    e.conf.edit(101, RUNBOOKS)  # rev 2 shares its bytes with page 102
    e.sync()
    e.conf.edit(101, "<p>v3</p>")
    e.sync()
    set_retention(e, keep_revisions=1, superseded_days=1, deleted_days=30)
    owner_sql(
        owner_engine,
        "UPDATE source_revisions SET fetched_at = now() - interval '2 days' WHERE tenant_id=:t",
        t=e.tenant,
    )
    store = blobs.get_store()
    v1 = blobs.blob_key(e.tenant, page(e, "101").json()["lineage"][0]["content_sha256"])
    counts = retention.purge_project(e.tenant, uuid.UUID(e.pid), store, datetime.now(UTC))
    assert counts["revisions_purged"] == 2 and counts["blobs_deleted"] == 1  # v1 only: v2 is page 102's too
    lineage = page(e, "101").json()["lineage"]
    assert [(r["seq"], r["stored"]) for r in lineage] == [(1, False), (2, False), (3, True)]
    r = page(e, "101", "/revisions/1/raw")
    assert r.status_code == 409 and r.json()["reason"] == "PURGED"
    with pytest.raises(blobs.BlobMissing):
        store.get(v1)
    assert page(e, "102", "/revisions/1/raw").text == RUNBOOKS  # the shared blob survived
    assert page(e, "101", "/revisions/3/raw").text == "<p>v3</p>"
    again = retention.purge_project(e.tenant, uuid.UUID(e.pid), store, datetime.now(UTC))
    assert again["revisions_purged"] == 0 and again["blobs_deleted"] == 0


def test_deleted_and_removed_items_are_purged_after_the_deletion_window(selected, owner_engine):
    e = selected
    e.conf.pages[100].status = "trashed"
    e.sync()
    store, now = blobs.get_store(), datetime.now(UTC)
    assert (
        retention.purge_project(e.tenant, uuid.UUID(e.pid), store, now)["revisions_purged"] == 0
    )  # within 30 days
    owner_sql(
        owner_engine,
        "UPDATE source_objects SET deleted_at = now() - interval '31 days' "
        "WHERE tenant_id=:t AND status='DELETED'",
        t=e.tenant,
    )
    assert retention.purge_project(e.tenant, uuid.UUID(e.pid), store, now)["revisions_purged"] == 1
    assert page(e, "100").json()["lineage"][0]["stored"] is False
    # Removing the space from the project marks its items deleted, which starts their clock.
    assert (
        e.client.delete(f"/v1/projects/{e.pid}/sources/confluence/{e.cid}:ENG", headers=e.h()).status_code
        == 204
    )
    counts = retention.purge_project(e.tenant, uuid.UUID(e.pid), store, now)
    assert counts["items_marked_deleted"] == 2 and counts["revisions_purged"] == 0
    assert page(e, "101").json()["status"] == "DELETED"


def test_unreferenced_blobs_are_collected_only_after_the_grace_period(selected, monkeypatch):
    e = selected
    store = blobs.get_store()
    _, orphan, _ = blobs.put(store, e.tenant, b"written by a sync that rolled back", "text/plain")
    now = datetime.now(UTC)
    assert retention.sweep_tenant(e.tenant, store, now)["orphans_deleted"] == 0  # too fresh
    monkeypatch.setattr(get_settings(), "content_orphan_grace_seconds", 0)
    assert retention.sweep_tenant(e.tenant, store, datetime.now(UTC))["orphans_deleted"] == 1
    with pytest.raises(blobs.BlobMissing):
        store.get(orphan)
    assert page(e, "101", "/revisions/1/raw").status_code == 200  # referenced blobs untouched


def test_the_worker_schedules_one_purge_per_project_with_content_whatever_its_sources_do(
    selected, owner_engine
):
    """INPROOM-162: the schedule tick queues due purges itself, also after the last source is removed."""
    from app.sync import worker

    e = selected
    jobs = "SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND source_kind='purge' AND status=:s"
    assert worker.schedule_tick(e.tenant)["purges"] == 1
    assert worker.schedule_tick(e.tenant)["purges"] == 0  # coalesced, and not due again for a day
    assert e.q(jobs, s="QUEUED") == 1
    while (out := worker.run_once("w", transports={}, schedule=False, tenant=e.tenant)) is not None:
        if out["source_kind"] == "purge":
            assert out["status"] == "SUCCEEDED"
    assert e.q(jobs, s="SUCCEEDED") == 1
    # The last source goes away: nothing syncs any more, but retention still runs when due.
    assert (
        e.client.delete(f"/v1/projects/{e.pid}/sources/confluence/{e.cid}:ENG", headers=e.h()).status_code
        == 204
    )
    owner_sql(
        owner_engine,
        "UPDATE source_sync_state SET next_due_at = now() WHERE tenant_id=:t AND source_kind='purge'",
        t=e.tenant,
    )
    assert worker.schedule_tick(e.tenant)["purges"] == 1
    out = worker.run_once("w", transports={}, schedule=False, tenant=e.tenant)
    assert out["source_kind"] == "purge" and out["result"]["items_marked_deleted"] == 3


def test_projects_without_stored_content_get_no_purge(env):
    from app.sync import worker

    assert worker.schedule_tick(env.tenant)["purges"] == 0


def test_object_store_outage_during_a_sync_retries_and_recovers_exactly(selected, monkeypatch):
    e = selected
    e.conf.edit(101, "<p>during an outage</p>")
    down = blobs.MemoryStore()
    down.fail = blobs.ObjectStoreUnavailable("connection refused")
    monkeypatch.setattr(confluence_sources, "get_store", lambda: down)
    out = sync_worker(e)
    assert (out["status"], out["error"]) == ("QUEUED", "OBJECTSTORE_UNAVAILABLE")  # will retry
    assert (
        page(e, "101").json()["revisions"] == 1
    )  # nothing partial: catalog and content rolled back together
    assert e.q("SELECT current_version FROM confluence_pages WHERE tenant_id=:t AND page_id='101'") == 3
    monkeypatch.undo()
    due(e.tenant)
    assert run_sync({"confluence": e.conf.transport()}, e.tenant)["status"] == "SUCCEEDED"
    assert [r["seq"] for r in page(e, "101").json()["lineage"]] == [1, 2]


def test_a_crash_after_the_blob_write_leaves_no_row_and_the_retry_records_once(selected, monkeypatch):
    e = selected
    e.conf.edit(101, "<p>crash between blob and commit</p>")
    from app.content import derivatives

    def crash(*args, **kwargs):
        raise RuntimeError("worker killed before commit")

    monkeypatch.setattr(derivatives, "on_new_revision", crash)
    out = sync_worker(e)
    assert out["status"] == "QUEUED" and out["error"] == "INTERNAL_ERROR"
    key = blobs.blob_key(e.tenant, blobs.sha256(b"<p>crash between blob and commit</p>"))
    assert blobs.read(blobs.get_store(), key)  # the blob was written ...
    assert page(e, "101").json()["revisions"] == 1  # ... but no row points at it yet
    monkeypatch.undo()
    due(e.tenant)
    assert run_sync({"confluence": e.conf.transport()}, e.tenant)["status"] == "SUCCEEDED"
    lineage = page(e, "101").json()["lineage"]
    assert [r["seq"] for r in lineage] == [1, 2] and lineage[1]["content_sha256"] == key.rsplit("/", 1)[1]


def test_an_item_restored_after_its_purge_is_readable_rebuilt_and_kept(selected, owner_engine):
    """INPROOM-161: restore after the deletion-window purge, then purge again."""
    e = selected
    original = page(e, "101", "/revisions/1/raw").content
    e.conf.pages[101].status = "trashed"
    e.sync()
    owner_sql(
        owner_engine,
        "UPDATE source_objects SET deleted_at = now() - interval '31 days' "
        "WHERE tenant_id=:t AND status='DELETED'",
        t=e.tenant,
    )
    store = blobs.get_store()
    assert retention.purge_project(e.tenant, uuid.UUID(e.pid), store, datetime.now(UTC))["blobs_deleted"] == 1
    e.conf.pages[101].status = "current"
    e.sync()
    obj = page(e, "101").json()
    assert (obj["status"], obj["revisions"]) == ("CURRENT", 2)
    assert [(r["seq"], r["parent_seq"], r["stored"]) for r in obj["lineage"]] == [
        (1, None, False),
        (2, 1, True),
    ]
    assert page(e, "101", "/revisions/2/raw").content == original
    derived = e.client.get(
        f"/v1/projects/{e.pid}/content/confluence/{e.sid}/101/revisions/2/derivatives", headers=e.h()
    ).json()["items"]
    assert [d["status"] for d in derived] == ["PENDING"]  # rebuilt by the next derive job
    again = retention.purge_project(e.tenant, uuid.UUID(e.pid), store, datetime.now(UTC))
    assert again["revisions_purged"] == 0 and page(e, "101", "/revisions/2/raw").content == original


def test_a_tenant_whose_first_ingestion_rolled_back_is_swept_by_the_worker(env, make_tenant, monkeypatch):
    """INPROOM-162 retest: no content row exists anywhere, yet the daily schedule reaches the orphan blob."""
    e = env
    store = blobs.get_store()
    with pytest.raises(RuntimeError), tenant_session(e.tenant) as conn:
        record_revision(
            conn,
            store,
            tenant=e.tenant,
            project_id=uuid.UUID(e.pid),
            source_kind="confluence",
            source_id=uuid.uuid4(),
            external_id="1",
            data=b"first ingestion, rolled back",
            media_type="text/plain",
        )
        raise RuntimeError("crash before commit")
    orphan = blobs.blob_key(e.tenant, blobs.sha256(b"first ingestion, rolled back"))
    assert e.q("SELECT count(*) FROM source_objects WHERE tenant_id=:t") == 0 and store.get(orphan)
    other = make_tenant()  # another tenant's (unreferenced, old) blob is never touched by this sweep
    _, theirs, _ = blobs.put(store, other, b"someone else's bytes", "text/plain")

    assert worker.schedule_tick(e.tenant)["sweeps"] == 1
    assert worker.schedule_tick(e.tenant)["sweeps"] == 0  # one per tenant per interval, coalesced
    out = worker.run_once("w", transports={}, schedule=False, tenant=e.tenant)
    assert out["source_kind"] == "sweep" and out["result"]["orphans_deleted"] == 0  # within the grace period
    store.get(orphan)

    monkeypatch.setattr(get_settings(), "content_orphan_grace_seconds", 0)  # the blob is "old" now
    with get_engine().begin() as conn:  # and a day has passed
        conn.execute(
            text(
                "UPDATE source_sync_state SET next_due_at = now() "
                "WHERE source_kind='sweep' AND tenant_id=:t"
            ),
            {"t": e.tenant},
        )
    assert worker.schedule_tick(e.tenant)["sweeps"] == 1
    out = worker.run_once("w", transports={}, schedule=False, tenant=e.tenant)
    assert out["status"] == "SUCCEEDED" and out["result"]["orphans_deleted"] == 1
    with pytest.raises(blobs.BlobMissing):
        store.get(orphan)
    assert store.get(theirs) == b"someone else's bytes"
