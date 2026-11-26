"""INPROOM-211/212 (story INPROOM-33 AC 1, 2, 3, 6, 7): keyword, semantic and hybrid retrieval - exact
identifiers, phrases, word forms, rank fusion, dedupe, caps, explanations, degradation; visible only."""

import json

import pytest

from app.content import embeddings
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import settings, upload
from tests.test_provenance_integration import resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected

ERRORS = b"# Error codes\n\nE1042 means the ledger lock timed out. Retry after the lock is released."
DEPLOY = b"# Deploying\n\nRestarting the API server clears the cache. See PAY-17 for the incident."


def retrieve(e, query, status=200, **body):
    body = {"query": query, "mode": "keyword"} | body
    r = e.client.post(f"/v1/projects/{e.pid}/knowledge/retrieve", json=body, headers=e.h())
    assert r.status_code == status, r.text
    return r.json()


def found(e, query, **body):
    return [h["citation"]["external_id"] for h in retrieve(e, query, **body)["items"]]


@pytest.fixture
def docs(selected):
    e = selected
    errors = upload(e, "errors.md", ERRORS).json()["document_id"]
    deploy = upload(e, "deploy.md", DEPLOY).json()["document_id"]
    drain(e)
    return e, errors, deploy


def test_identifiers_phrases_word_forms_and_exclusions(docs):
    e, errors, deploy = docs
    result = retrieve(e, "E1042")
    assert result["mode"] == "keyword" and result["query_terms"] == ["E1042"] and result["model"] is None
    [hit] = result["items"]
    assert hit["citation"]["external_id"] == errors and hit["title"] == "errors.md"
    assert hit["explanation"]["keyword"]["rank"] == 1 and hit["explanation"]["keyword"]["terms"] == ["E1042"]
    assert hit["explanation"]["keyword"]["score"] > 0 and hit["provenance"]["freshness"] == "current"
    [cited] = resolve(e, hit["citation"])["results"]  # the citation resolves (INPROOM-30)
    assert cited["error"] is None

    assert found(e, "PAY-17") == [deploy]  # issue keys match verbatim
    # word forms: "restart" finds "Restart the api." (page 102) and "Restarting the API"; both terms matched
    hits = retrieve(e, "restart api")["items"]
    assert {h["citation"]["external_id"] for h in hits} >= {"102", deploy}
    assert all(h["explanation"]["keyword"]["terms"] == ["restart", "api"] for h in hits[:2])
    assert [h["explanation"]["keyword"]["rank"] for h in hits] == list(range(1, len(hits) + 1))
    assert found(e, '"ledger lock"') == [errors] and found(e, '"lock ledger"') == []  # phrases keep order
    assert "102" in found(e, "restart -cache") and deploy not in found(e, "restart -cache")
    assert found(e, "restart", kinds=["upload"]) == [deploy]
    assert found(e, "Runbooks") == ["102"]  # titles and headings count
    stop = retrieve(e, "the")  # stop words alone match nothing, and are reported as ignored
    assert (stop["items"], stop["query_terms"], stop["ignored_terms"]) == ([], [], ["the"])
    assert retrieve(e, "restart the api")["query_terms"] == ["restart", "api"]
    assert found(e, "errors.md") == [errors]  # an upload's title, though no heading carries it
    # OR-ed units rank chunks matching more of them first
    assert found(e, "ledger restarting released")[0] == errors

    for bad, code in (
        ({"query": "-cache"}, "QUERY_HAS_NO_TERMS"),
        ({"query": ""}, None),
        ({"query": "x", "top_k": 0}, None),
    ):
        r = e.client.post(f"/v1/projects/{e.pid}/knowledge/retrieve", json=bad, headers=e.h())
        assert r.status_code == 422 and (code is None or r.json()["code"] == code), (bad, r.text)


MODES = ("keyword", "semantic", "hybrid")


def everywhere(e, query, **body):
    """The external ids found in any mode (top 50, evidence or not: leaks are about candidates)."""
    return set().union(*(found(e, query, mode=m, top_k=50, include_unsupported=True, **body) for m in MODES))


def test_withheld_superseded_and_paused_content_is_never_retrieved(docs):
    e, errors, _ = docs
    assert {"101", "102"} <= everywhere(e, "restart the api architecture")
    e.conf.pages[102].groups = ["bots"]  # RESTRICTED at the source
    e.conf.pages[101].add_version("<h1>Architecture</h1><p>Event sourcing with queues.</p>")  # superseded
    assert e.sync().status_code == 200
    drain(e)
    assert "102" not in everywhere(e, "restart the api")
    for mode in MODES:
        hits = retrieve(e, "architecture event sourcing", mode=mode, top_k=50, include_unsupported=True)[
            "items"
        ]
        assert {h["citation"]["seq"] for h in hits if h["citation"]["external_id"] == "101"} == {2}, mode

    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # SOURCE_PAUSED
    for mode in MODES:
        hits = retrieve(e, "architecture", mode=mode, top_k=50, include_unsupported=True)["items"]
        assert all(h["citation"]["kind"] != "confluence" for h in hits), mode
    settings(e, allowed_kinds=["git", "jira", "confluence"])  # uploads disabled
    assert errors not in everywhere(e, "E1042 ledger lock")
    settings(e, allowed_kinds=["git", "jira", "confluence", "upload"])
    assert found(e, "E1042") == [errors]


SHARED = b"# Key rotation\n\nRotate the signing keys every ninety days and revoke the old ones."
QUOTAS = b"\n\n".join(
    f"## Quota {i}\n\nThe quota for tier {i} is enforced per tenant.".encode() for i in range(5)
)


def test_hybrid_fuses_dedupes_caps_and_explains(docs):
    e, errors, deploy = docs
    one = upload(e, "keys-a.md", SHARED).json()["document_id"]
    two = upload(e, "keys-b.md", SHARED).json()["document_id"]
    quotas = upload(e, "quotas.md", b"# Quotas" + b"\n\n" + QUOTAS).json()["document_id"]
    drain(e)

    result = retrieve(e, "restart the api", mode="hybrid")
    assert (result["mode"], result["model"], result["degraded"]) == ("hybrid", "local-hash-v1", [])
    top = result["items"][0]
    assert top["citation"]["external_id"] in {"102", deploy}
    explained = top["explanation"]
    assert explained["keyword"]["rank"] >= 1 and explained["semantic"]["rank"] >= 1  # found by both
    assert explained["rrf"] == round(
        1 / (60 + explained["keyword"]["rank"]) + 1 / (60 + explained["semantic"]["rank"]), 6
    )
    assert [h["explanation"]["rrf"] for h in result["items"]] == sorted(
        (h["explanation"]["rrf"] for h in result["items"]), reverse=True
    )
    # an exact code ranks first in hybrid even though the semantic list alone may not put it there
    assert found(e, "E1042", mode="hybrid")[0] == errors

    # identical text in two documents: returned once, the other location in also_in
    hits = [
        h
        for h in retrieve(e, "rotate signing keys", mode="hybrid")["items"]
        if h["citation"]["external_id"] in {one, two}
    ]
    assert len(hits) == 1 and [x["citation"]["external_id"] for x in hits[0]["also_in"]] == [
        ({one, two} - {hits[0]["citation"]["external_id"]}).pop()
    ]
    # five chunks of one document: at most max_per_document (default 3)
    assert found(e, "quota tier tenant", mode="hybrid", top_k=20).count(quotas) == 3
    assert found(e, "quota tier tenant", mode="hybrid", top_k=20, max_per_document=1).count(quotas) == 1
    # exclusions apply to the semantic candidates as well
    assert deploy not in found(e, "restart the api -cache", mode="semantic", top_k=50)


def test_an_embedding_outage_degrades_hybrid_to_keyword(docs, monkeypatch, caplog):
    e, errors, _ = docs

    class Down:
        model, version, dimensions, batch_size = "local-hash-v1", "1", 256, 64

        def embed(self, texts, purpose="document"):
            raise embeddings.EmbeddingUnavailable("connection refused")

    monkeypatch.setattr(embeddings, "get_embedder", lambda: Down())
    with caplog.at_level("INFO", logger="app.modules.knowledge"):
        result = retrieve(e, "E1042", mode="hybrid")
    assert (result["degraded"], result["model"]) == (["semantic"], None)
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("knowledge retrieve")]
    logged = json.loads(line.split(" ", 3)[3])  # telemetry: counts and verdicts, never the query text
    assert logged["degraded"] == ["semantic"] and logged["evidence"] == "SUFFICIENT" and "E1042" not in line
    assert logged["candidates"] == {"keyword": 1, "semantic": 0} and logged["ms"] >= 0
    assert [h["citation"]["external_id"] for h in result["items"]] == [errors]
    assert result["items"][0]["explanation"]["semantic"] is None
    r = e.client.post(
        f"/v1/projects/{e.pid}/knowledge/retrieve", json={"query": "E1042", "mode": "semantic"}, headers=e.h()
    )
    assert r.status_code == 503 and r.json()["code"] == "EMBEDDING_UNAVAILABLE" and r.headers["Retry-After"]
    assert retrieve(e, "E1042", mode="keyword")["degraded"] == []
