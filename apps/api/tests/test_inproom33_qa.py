"""QA probes for INPROOM-33: a rejected query embedding (INPROOM-219) and a zero-feature semantic query with
include_unsupported (INPROOM-220). Committed from QA's tests/qa_inproom3334.py."""

import pytest

from app.content import embeddings
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain

env, selected = conf.env, conf.selected
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", ["hybrid", "semantic"])
def test_rejected_embedding_is_a_controlled_retrieval_error(selected, monkeypatch, mode):
    e = selected

    class Rejected:
        def embed(self, texts, purpose="document"):
            raise embeddings.EmbeddingRejected("provider rejected the query")

    monkeypatch.setattr(embeddings, "get_embedder", lambda: Rejected())
    monkeypatch.setattr(e.client._transport, "raise_server_exceptions", False)
    response = e.client.post(
        f"/v1/projects/{e.pid}/knowledge/retrieve",
        json={"query": "architecture", "mode": mode},
        headers=e.h(),
    )
    assert response.status_code in (200, 422, 503), (mode, response.status_code, response.text)


@pytest.mark.parametrize(
    "include_unsupported",
    [
        False,
        True,
    ],
)
def test_unicode_semantic_query_does_not_crash(selected, monkeypatch, include_unsupported):
    e = selected
    drain(e)
    monkeypatch.setattr(e.client._transport, "raise_server_exceptions", False)
    response = e.client.post(
        f"/v1/projects/{e.pid}/knowledge/retrieve",
        json={"query": "你好", "mode": "semantic", "include_unsupported": include_unsupported},
        headers=e.h(),
    )
    assert response.status_code in (200, 422), (response.status_code, response.text)
