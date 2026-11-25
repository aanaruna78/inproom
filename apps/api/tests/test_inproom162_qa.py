"""QA retest probe for INPROOM-162 (build/qa/INPROOM-27/retest/test_orphan_only.py at 0345082).

Can the daily collector reach a blob left by the very first failed ingestion (no content rows at all)?
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.content import retention
from app.content import store as blobs
from app.content.revisions import record_revision
from app.db import tenant_session
from app.sync import worker
from tests import test_confluence_sources_integration as h

env = h.env
pytestmark = pytest.mark.integration


def test_initial_ingest_rollback_orphan_is_reachable_by_daily_cleanup(env, monkeypatch):
    e = env
    data = b"first ingestion wrote this blob, then failed before committing any content row"
    store = blobs.get_store()
    with pytest.raises(RuntimeError, match="before commit"):
        with tenant_session(e.tenant) as conn:
            record_revision(
                conn,
                store,
                tenant=e.tenant,
                project_id=uuid.UUID(e.pid),
                source_kind="confluence",
                source_id=uuid.uuid4(),
                external_id="101",
                data=data,
                media_type="text/plain",
            )
            raise RuntimeError("before commit")
    key = blobs.blob_key(e.tenant, blobs.sha256(data))
    assert store.get(key) == data
    assert e.q("SELECT count(*) FROM source_objects WHERE tenant_id=:t") == 0
    # Simulate a blob older than the grace period without sleeping a day.
    original_list = store.list

    def aged(prefix):
        for item in original_list(prefix):
            yield blobs.BlobInfo(item.key, item.size, datetime.now(UTC) - timedelta(days=2))

    monkeypatch.setattr(store, "list", aged)
    results = []
    for _ in range(10):
        out = worker.run_once("qa-orphan", transports={}, tenant=e.tenant)
        if out is None:
            break
        results.append(out)
    remaining = list(store.list(blobs.tenant_prefix(e.tenant)))
    # Adapted control (QA asserted that a MANUAL run still found the orphan, which only holds while the
    # scheduler cannot reach it). Orphan collection is now the per-tenant sweep, and the scheduled one has
    # already collected it: a manual sweep finds nothing left.
    manual = retention.sweep_tenant(e.tenant, store, datetime.now(UTC))
    assert manual["orphans_deleted"] == 0
    assert key not in [
        item.key for item in remaining
    ], f"first-ingest orphan never scheduled for collection; worker results={results}; manual purge={manual}"
