"""INPROOM-205 (story INPROOM-32 AC 3, 4): semantic search returns relevant chunks with their source version
and citation - only from visible current revisions, embedded with the active model."""

import pytest

from app.config import get_settings
from app.content import embeddings
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import settings, upload
from tests.test_provenance_integration import resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def search(e, query, **body):
    r = e.client.post(f"/v1/projects/{e.pid}/knowledge/search", json={"query": query} | body, headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def found(e, query, **body):
    return [h["citation"]["external_id"] for h in search(e, query, **body)["items"]]


def test_relevant_chunks_come_with_versions_and_citations(selected):
    e = selected
    drain(e)
    result = search(e, "restart the api", top_k=3)
    assert (result["model"], result["dimensions"]) == ("local-hash-v1", 256)
    top = result["items"][0]
    assert top["citation"]["external_id"] == "102" and top["title"] == "Runbooks"
    assert top["citation"]["seq"] == 1 and top["retrieved_at"] and 0 < top["score"] <= 1
    assert [h["score"] for h in result["items"]] == sorted(
        (h["score"] for h in result["items"]), reverse=True
    )
    assert top["provenance"]["freshness"] == "current" and top["provenance"]["trust"] == "trusted"
    [cited] = resolve(e, top["citation"])["results"]  # the citation resolves (INPROOM-30)
    assert cited["error"] is None and cited["record"]["source"]["title"] == "Runbooks"
    assert found(e, "restart the api", kinds=["jira"]) == []  # filters
    assert found(e, "restart the api", source_ids=[e.sid], top_k=1) == ["102"]
    for bad in ({"query": ""}, {"query": "x", "top_k": 51}, {"query": "x", "kinds": ["svn"]}):
        r = e.client.post(f"/v1/projects/{e.pid}/knowledge/search", json=bad, headers=e.h())
        assert r.status_code == 422, bad


def test_withheld_superseded_and_other_model_content_is_never_returned(selected, monkeypatch):
    e = selected
    doc = upload(e, "notes.md", b"# Notes\n\nThe deployment checklist lives here.").json()["document_id"]
    drain(e)
    assert "102" in found(e, "restart the api") and doc in found(e, "deployment checklist")

    e.conf.pages[102].groups = ["bots"]  # RESTRICTED at the source
    e.conf.pages[101].add_version("<h1>Architecture</h1><p>Event sourcing with queues.</p>")  # superseded
    assert e.sync().status_code == 200
    drain(e)
    assert "102" not in found(e, "restart the api", top_k=50)
    hits = search(e, "event sourcing with queues", top_k=50)["items"]
    assert {h["citation"]["seq"] for h in hits if h["citation"]["external_id"] == "101"} == {
        2
    }  # current only

    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # SOURCE_PAUSED
    assert all(h["citation"]["kind"] != "confluence" for h in search(e, "architecture", top_k=50)["items"])
    assert e.client.post(f"{base}/include", headers=e.h()).status_code == 200

    settings(e, allowed_kinds=["git", "jira", "confluence"])  # uploads disabled
    assert doc not in found(e, "deployment checklist", top_k=50)
    settings(e, allowed_kinds=["git", "jira", "confluence", "upload"])
    assert doc in found(e, "deployment checklist")

    put = e.client.put(
        f"{base}/policy",
        json={"tier": "trusted", "classification": "confidential", "reason": "secret"},
        headers=e.h(**{"If-Match": '"0"'}),
    )
    assert put.status_code == 200  # over the project cap: withheld
    assert all(h["citation"]["kind"] != "confluence" for h in search(e, "architecture", top_k=50)["items"])

    monkeypatch.setenv("INPROOM_EMBEDDING_MODEL", "another-model")  # vectors of another model never mix
    get_settings.cache_clear()
    embeddings.get_embedder.cache_clear()
    other = search(e, "deployment checklist")
    assert other["model"] == "another-model" and other["items"] == []


def test_a_provider_outage_is_a_retryable_503(selected, monkeypatch):
    e = selected

    class Down:
        model, version, dimensions, batch_size = "local-hash-v1", "1", 256, 64

        def embed(self, texts, purpose="document"):
            raise embeddings.EmbeddingUnavailable("connection refused")

    monkeypatch.setattr(embeddings, "get_embedder", lambda: Down())
    r = e.client.post(f"/v1/projects/{e.pid}/knowledge/search", json={"query": "x"}, headers=e.h())
    assert r.status_code == 503 and r.json()["code"] == "EMBEDDING_UNAVAILABLE"
    assert r.headers["Retry-After"] == "30"
