"""INPROOM-206 (story INPROOM-32 AC 5, 6, 7): operating the vector index - accurate status, listed failures,
a safe model switch (both models coexist; search uses the active one), chunk rebuilds, and vectors deleted
with the content they embed."""

import uuid
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.content import embed as embed_job
from app.content import embeddings, retention
from app.content import store as blobs
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_embed_integration import Flaky

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def status(e):
    r = e.client.get(f"/v1/projects/{e.pid}/knowledge/index", headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def reindex(e, scope, reason="model switch"):
    return e.client.post(
        f"/v1/projects/{e.pid}/knowledge/reindex", json={"scope": scope, "reason": reason}, headers=e.h()
    )


def search(e, query):
    r = e.client.post(f"/v1/projects/{e.pid}/knowledge/search", json={"query": query}, headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def use_model(monkeypatch, model):
    monkeypatch.setenv("INPROOM_EMBEDDING_MODEL", model)
    get_settings.cache_clear()
    embeddings.get_embedder.cache_clear()


def test_status_and_a_safe_model_switch(selected, monkeypatch):
    e = selected
    drain(e)
    before = status(e)
    assert (before["provider"], before["model"], before["dimensions"]) == ("local", "local-hash-v1", 256)
    assert before["chunks"] > 0 and before["embedded"] == before["chunks"]
    assert (before["pending"], before["failed"]) == (0, 0)
    assert before["derivatives"] == {"READY": before["derivatives"]["READY"]}
    assert before["models"] == [{"model": "local-hash-v1", "vectors": before["chunks"]}]

    use_model(monkeypatch, "local-hash-v2")  # the operator switches models
    switched = status(e)
    assert (switched["model"], switched["embedded"], switched["pending"]) == (
        "local-hash-v2",
        0,
        before["chunks"],
    )
    assert search(e, "restart the api")["items"] == []  # never answered with the other model's vectors
    r = reindex(e, "embeddings")
    assert r.status_code == 202 and r.json()["model"] == "local-hash-v2"
    assert reindex(e, "embeddings").status_code == 202  # coalesced with the queued job
    assert (
        e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND source_kind='embed' AND status='QUEUED'")
        == 1
    )
    drain(e)
    done = status(e)
    assert done["embedded"] == done["chunks"] and done["pending"] == 0
    assert [m["model"] for m in done["models"]] == ["local-hash-v1", "local-hash-v2"]  # both coexist
    assert search(e, "restart the api")["items"][0]["citation"]["external_id"] == "102"
    assert e.q("SELECT count(*) FROM audit_events WHERE tenant_id=:t AND action='knowledge.reindex'") == 2
    assert reindex(e, "embeddings", reason="").status_code == 422


def test_failures_are_listed_and_a_reindex_retries_them(selected, monkeypatch):
    e = selected
    Flaky.calls = 1  # skip the simulated outage: reject only
    monkeypatch.setattr(embed_job, "get_embedder", lambda: Flaky())
    drain(e)
    failures = e.client.get(f"/v1/projects/{e.pid}/knowledge/index/failures", headers=e.h()).json()
    assert [(f["citation"]["external_id"], f["code"], f["attempts"]) for f in failures] == [
        ("102", "EMBEDDING_REJECTED", 1)
    ]
    assert "HTTP 400" in failures[0]["detail"] and status(e)["failed"] == 1
    monkeypatch.setattr(embed_job, "get_embedder", lambda: embeddings.LocalEmbedder())  # fixed
    assert reindex(e, "embeddings", reason="provider fixed").json()["failures_cleared"] == 1
    drain(e)
    fixed = status(e)
    assert (fixed["failed"], fixed["pending"]) == (0, 0)


def test_chunk_rebuilds_and_vectors_are_deleted_with_their_content(selected):
    e = selected
    drain(e)
    ready = status(e)["derivatives"]["READY"]
    rebuilt = reindex(e, "chunks", reason="chunker upgrade")
    assert rebuilt.status_code == 202 and rebuilt.json()["chunks_invalidated"] == ready
    drain(e)
    after = status(e)
    assert after["derivatives"] == {"INVALIDATED": ready, "READY": ready}  # rebuilt, old outputs retired
    assert after["embedded"] == after["chunks"]  # identical text: the existing vectors are reused

    e.conf.pages[101].add_version("<h1>Architecture</h1><p>Rewritten entirely.</p>")
    assert e.sync().status_code == 200
    drain(e)  # the old revision's chunks left with its derivative: their vectors are orphans now
    assert orphans(e) >= 1
    counts = retention.purge_project(e.tenant, uuid.UUID(e.pid), blobs.get_store(), datetime.now(UTC))
    assert counts["vectors_deleted"] >= 1 and orphans(e) == 0
    assert status(e)["embedded"] == status(e)["chunks"]  # every current chunk keeps its vector


def orphans(e):
    return e.q(
        "SELECT count(*) FROM chunk_embeddings c WHERE c.tenant_id=:t AND NOT EXISTS "
        "(SELECT 1 FROM knowledge_chunks k WHERE k.tenant_id = c.tenant_id AND k.sha256 = c.sha256)"
    )
