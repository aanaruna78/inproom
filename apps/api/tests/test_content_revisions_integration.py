"""INPROOM-157 (story INPROOM-27 AC 1, 2, 4): source objects and their revision chains, on PostgreSQL + the
S3-compatible object store (SeaweedFS locally and in CI)."""

import threading
import uuid

import pytest
from sqlalchemy import select, text

from app.content import store as blobs
from app.content.revisions import record_revision, revision_bytes
from app.db import tenant_session
from app.db_schema import outbox_events, source_objects, source_revisions

pytestmark = pytest.mark.integration


@pytest.fixture
def s3():
    store = blobs.get_store()
    assert isinstance(store, blobs.S3Store)
    return store


def _project(owner_engine, tenant):
    w, p = uuid.uuid4(), uuid.uuid4()
    with owner_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, slug, name, created_by) VALUES (:w,:t,'ws','WS','u')"
            ),
            {"w": w, "t": tenant},
        )
        c.execute(
            text(
                "INSERT INTO projects (id, tenant_id, workspace_id, key, name, mode, status, owner_id) "
                "VALUES (:p,:t,:w,:k,'Proj','NEW','ACTIVE','u')"
            ),
            {"p": p, "t": tenant, "w": w, "k": "P" + uuid.uuid4().hex[:6].upper()},
        )
    return p


@pytest.fixture
def ctx(owner_engine, make_tenant, s3):
    tenant = make_tenant()

    class Ctx:
        pass

    c = Ctx()
    c.tenant, c.project, c.source, c.store = tenant, _project(owner_engine, tenant), uuid.uuid4(), s3

    def record(data: bytes, external_id="100", **kw):
        with tenant_session(c.tenant) as conn:
            return record_revision(
                conn,
                c.store,
                tenant=c.tenant,
                project_id=c.project,
                source_kind="confluence",
                source_id=c.source,
                external_id=external_id,
                data=data,
                media_type="application/vnd.atlassian.confluence.storage+xml",
                title="Home",
                **kw,
            )

    def revisions(object_id):
        with tenant_session(c.tenant) as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(source_revisions)
                    .where(source_revisions.c.object_id == object_id)
                    .order_by(source_revisions.c.seq)
                ).mappings()
            ]

    c.record, c.revisions = record, revisions
    return c


def test_changes_append_one_revision_each_and_unchanged_content_appends_none(ctx):
    acl = {"space": "ENG", "restrictions": {"read": []}}
    job = uuid.uuid4()
    first = ctx.record(b"<p>v1</p>", provider_version="1", acl=acl, job_id=job)
    assert first.created and first.seq == 1
    assert not ctx.record(b"<p>v1</p>", provider_version="1").created  # a re-sync
    second = ctx.record(b"<p>v2</p>", provider_version="2", acl=acl)
    assert second.created and second.seq == 2
    revs = ctx.revisions(first.object_id)
    assert [(r["seq"], r["provider_version"]) for r in revs] == [(1, "1"), (2, "2")]
    assert revs[1]["parent_revision_id"] == revs[0]["id"] and revs[0]["parent_revision_id"] is None
    assert (revs[0]["acl"], revs[0]["job_id"], revs[0]["size_bytes"]) == (acl, job, 9)
    assert revs[0]["blob_key"] == blobs.blob_key(ctx.tenant, blobs.sha256(b"<p>v1</p>"))
    assert revision_bytes(ctx.store, revs[0]) == b"<p>v1</p>"  # exact bytes, sha256 verified
    with tenant_session(ctx.tenant) as conn:
        query = select(source_objects).where(source_objects.c.id == first.object_id)
        obj = conn.execute(query).mappings().one()
        events = conn.execute(
            select(outbox_events.c.payload).where(outbox_events.c.type == "source.revision.created")
        ).scalars()
        assert sorted(e["seq"] for e in events) == [1, 2]
    assert (obj["current_revision_id"], obj["revisions"], obj["status"]) == (second.revision_id, 2, "CURRENT")


def test_a_revert_is_a_new_revision_that_reuses_the_stored_blob(ctx):
    a = ctx.record(b"A")
    ctx.record(b"B")
    again = ctx.record(b"A")
    revs = ctx.revisions(a.object_id)
    assert again.created and [r["seq"] for r in revs] == [1, 2, 3]
    assert revs[2]["blob_key"] == revs[0]["blob_key"] and revs[2]["parent_revision_id"] == revs[1]["id"]


def test_concurrent_recorders_of_the_same_change_append_exactly_one_revision(ctx):
    ctx.record(b"base")
    results, barrier = [], threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(ctx.record(b"changed"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.created for r in results) == 1
    assert [r["seq"] for r in ctx.revisions(results[0].object_id)] == [1, 2]


def test_oversized_content_is_recorded_without_storing_its_bytes(ctx, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "content_max_bytes", 8)
    big = ctx.record(b"0123456789")
    assert big.created and big.status == "TOO_LARGE"
    rev = ctx.revisions(big.object_id)[0]
    assert rev["blob_key"] is None and rev["size_bytes"] == 10
    with pytest.raises(blobs.BlobMissing):
        revision_bytes(ctx.store, rev)
    small = ctx.record(b"small")
    assert small.status == "CURRENT" and small.seq == 2


def test_identical_content_in_two_tenants_is_two_blobs_and_rows_stay_private(ctx, owner_engine, make_tenant):
    mine = ctx.record(b"shared text")
    other = make_tenant()
    other_project = _project(owner_engine, other)
    with tenant_session(other) as conn:
        theirs = record_revision(
            conn,
            ctx.store,
            tenant=other,
            project_id=other_project,
            source_kind="jira",
            source_id=uuid.uuid4(),
            external_id="10001",
            data=b"shared text",
            media_type="text/plain",
        )
        assert conn.execute(select(source_objects.c.id)).scalars().all() == [theirs.object_id]
        assert conn.execute(select(source_revisions.c.blob_key)).scalars().all() == [
            blobs.blob_key(other, mine.content_sha256)
        ]
    keys = {r["blob_key"] for r in ctx.revisions(mine.object_id)}
    assert keys == {blobs.blob_key(ctx.tenant, mine.content_sha256)}
    assert blobs.read(ctx.store, blobs.blob_key(other, mine.content_sha256)) == b"shared text"


def test_an_object_store_outage_writes_nothing(ctx):
    down = blobs.MemoryStore()
    down.fail = blobs.ObjectStoreUnavailable("connection refused")
    ctx.store = down
    with pytest.raises(blobs.ObjectStoreUnavailable):
        ctx.record(b"never stored")
    with tenant_session(ctx.tenant) as conn:
        assert conn.execute(select(source_objects.c.id)).first() is None
