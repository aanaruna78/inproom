"""QA probes for INPROOM-32: zero-feature queries (INPROOM-207), malformed provider vectors (INPROOM-208)
and a rejected query embedding (INPROOM-209). Committed from QA's tests/qa_inproom303132.py."""

import math

import httpx
import pytest

from app.content.embeddings import EmbeddingRejected
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_embeddings_unit import adapter

env, selected = conf.env, conf.selected


@pytest.mark.integration
@pytest.mark.parametrize("query", ["!!!", "   ", "你好"])
def test_zero_feature_query_does_not_crash_search(selected, query, monkeypatch):
    e = selected
    drain(e)
    monkeypatch.setattr(e.client._transport, "raise_server_exceptions", False)
    response = e.client.post(f"/v1/projects/{e.pid}/knowledge/search", json={"query": query}, headers=e.h())
    assert response.status_code in (200, 422), response.text
    if response.status_code == 200:
        assert all(math.isfinite(hit["score"]) for hit in response.json()["items"])


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [float("nan"), 0, 1]}],
        [{"index": 0, "embedding": [float("inf"), 0, 1]}],
        [{"index": 0, "embedding": [0, 0, 0]}],
        [{"index": 0, "embedding": [1, 0, 0]}, {"index": 0, "embedding": [0, 1, 0]}],
        [None],
    ],
)
def test_malformed_vectors_rejected_before_storage(data):
    # Non-finite/zero vectors cannot support cosine search; duplicate indexes misassign text vectors.
    import json

    response = httpx.Response(
        200, content=json.dumps({"data": data}).encode(), headers={"content-type": "application/json"}
    )
    with pytest.raises(EmbeddingRejected):
        adapter(lambda request: response).embed(["input"] * len(data))


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="INPROOM-209 (QA): fixed in its own commit")
def test_rejected_query_has_a_controlled_api_error(selected, monkeypatch):
    from app.content import embeddings

    e = selected

    class Rejected:
        def embed(self, texts, purpose="document"):
            raise EmbeddingRejected("provider input limit exceeded")

    monkeypatch.setattr(embeddings, "get_embedder", lambda: Rejected())
    monkeypatch.setattr(e.client._transport, "raise_server_exceptions", False)
    response = e.client.post(
        f"/v1/projects/{e.pid}/knowledge/search", json={"query": "valid search"}, headers=e.h()
    )
    assert response.status_code in (422, 503), response.text
