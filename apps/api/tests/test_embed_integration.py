"""INPROOM-204 (story INPROOM-32 AC 2, 5): every chunk of visible current content gets one vector for the
active model - deduplicated by content within the tenant, never duplicated by a retry; outages retry with
nothing recorded, rejected inputs are isolated and listed, and recovery embeds them."""

import pytest
from sqlalchemy import text

from app.content import embed as embed_job
from app.content.embeddings import EmbeddingRejected, EmbeddingUnavailable, LocalEmbedder
from tests import test_confluence_sources_integration as conf
from tests.sync_helpers import due
from tests.test_document_api_integration import drain

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected

MODEL = "local-hash-v1"


def counts(e):
    return {
        "chunks": e.q(
            "SELECT count(DISTINCT k.sha256) FROM knowledge_chunks k JOIN source_objects o "
            "ON o.id = k.object_id AND o.current_revision_id = k.revision_id "
            "WHERE k.tenant_id=:t AND o.status = 'CURRENT'"
        ),
        "vectors": e.q("SELECT count(*) FROM chunk_embeddings WHERE tenant_id=:t AND model=:m", m=MODEL),
        "failures": e.q("SELECT count(*) FROM chunk_embedding_failures WHERE tenant_id=:t"),
    }


def test_visible_chunks_are_embedded_once(selected):
    e = selected
    drain(e)  # derive -> chunks -> embed (queued by derive)
    first = counts(e)
    assert first["chunks"] > 0 and first["vectors"] == first["chunks"] and first["failures"] == 0
    dims = e.q("SELECT DISTINCT dimensions FROM chunk_embeddings WHERE tenant_id=:t")
    assert (
        dims == 256 and e.q("SELECT DISTINCT model_version FROM chunk_embeddings WHERE tenant_id=:t") == "1"
    )
    with conf_session(e) as conn:
        embed_job.queue_embed(conn, e.tenant, e.pid, "manual")
    drain(e)
    assert counts(e)["vectors"] == first["vectors"]  # nothing left to embed, nothing duplicated
    e.conf.pages[102].groups = ["bots"]  # restricted: its chunks are no longer pending (or served)
    e.conf.pages[101].add_version("<h1>Architecture</h1><p>Brand new text about queues.</p>")
    assert e.sync().status_code == 200
    drain(e)
    assert missing(e) == 0  # the new revision's chunks are embedded; restricted 102 is simply not pending
    assert counts(e)["vectors"] > first["vectors"]


def missing(e):
    """Distinct chunks of CURRENT items' current revisions without a vector for the model."""
    return e.q(
        "SELECT count(DISTINCT k.sha256) FROM knowledge_chunks k JOIN source_objects o "
        "ON o.id = k.object_id AND o.current_revision_id = k.revision_id "
        "WHERE k.tenant_id=:t AND o.status = 'CURRENT' AND NOT EXISTS (SELECT 1 FROM chunk_embeddings c "
        "WHERE c.tenant_id = k.tenant_id AND c.model = :m AND c.sha256 = k.sha256)",
        m=MODEL,
    )


def conf_session(e):
    from app.db import tenant_session

    return tenant_session(e.tenant)


class Flaky(LocalEmbedder):
    """Down for the first call, then rejects any text mentioning 'Restart' (page 102)."""

    calls = 0

    def embed(self, texts, purpose="document"):
        Flaky.calls += 1
        if Flaky.calls == 1:
            raise EmbeddingUnavailable("connection refused")
        if any("Restart" in t for t in texts):
            raise EmbeddingRejected("embedding provider returned HTTP 400")
        return super().embed(texts, purpose)


def test_outages_retry_and_rejections_are_isolated(selected, monkeypatch):
    e = selected
    Flaky.calls = 0
    monkeypatch.setattr(embed_job, "get_embedder", lambda: Flaky())
    runs = drain(e)
    retried = [r for r in runs if r["source_kind"] == "embed" and r["status"] != "SUCCEEDED"]
    assert retried and retried[0]["error"] == "EMBEDDING_UNAVAILABLE"
    assert counts(e)["vectors"] == 0  # the outage recorded nothing
    due(e.tenant)
    drain(e)
    after = counts(e)
    assert after["failures"] >= 1 and after["vectors"] == after["chunks"] - after["failures"]
    reason = e.q("SELECT code FROM chunk_embedding_failures WHERE tenant_id=:t LIMIT 1")
    assert reason == "EMBEDDING_REJECTED"
    monkeypatch.setattr(embed_job, "get_embedder", lambda: LocalEmbedder())  # the provider recovers
    with conf_session(e) as conn:  # what a reindex of embeddings does (INPROOM-206)
        conn.execute(text("DELETE FROM chunk_embedding_failures"))
    with conf_session(e) as conn:
        embed_job.queue_embed(conn, e.tenant, e.pid, "manual")
    drain(e)
    final = counts(e)
    assert final["vectors"] == final["chunks"] and final["failures"] == 0
