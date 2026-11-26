"""INPROOM-211 (story INPROOM-33 AC 1, 2, 6): keyword retrieval finds exact identifiers, keys, phrases and
word forms, honours exclusions and filters, explains each hit - and only returns visible current content."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import settings, upload
from tests.test_provenance_integration import resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected

ERRORS = b"# Error codes\n\nE1042 means the ledger lock timed out. Retry after the lock is released."
DEPLOY = b"# Deploying\n\nRestarting the API server clears the cache. See PAY-17 for the incident."


def retrieve(e, query, status=200, **body):
    r = e.client.post(f"/v1/projects/{e.pid}/knowledge/retrieve", json={"query": query} | body, headers=e.h())
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
    assert result["mode"] == "keyword" and result["query_terms"] == ["E1042"]
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


def test_withheld_superseded_and_paused_content_is_never_retrieved(docs):
    e, errors, _ = docs
    assert "102" in found(e, "restart") and "101" in found(e, "architecture")
    e.conf.pages[102].groups = ["bots"]  # RESTRICTED at the source
    e.conf.pages[101].add_version("<h1>Architecture</h1><p>Event sourcing with queues.</p>")  # superseded
    assert e.sync().status_code == 200
    drain(e)
    assert "102" not in found(e, "restart", top_k=50)
    assert {
        h["citation"]["seq"]
        for h in retrieve(e, "architecture", top_k=50)["items"]
        if h["citation"]["external_id"] == "101"
    } == {2}

    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # SOURCE_PAUSED
    assert all(h["citation"]["kind"] != "confluence" for h in retrieve(e, "architecture", top_k=50)["items"])
    settings(e, allowed_kinds=["git", "jira", "confluence"])  # uploads disabled
    assert found(e, "E1042") == []
    settings(e, allowed_kinds=["git", "jira", "confluence", "upload"])
    assert found(e, "E1042") == [errors]
