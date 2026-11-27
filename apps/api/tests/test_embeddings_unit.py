"""INPROOM-204 (story INPROOM-32 AC 2, 5): the embedding gateway - a deterministic local embedder, and an
OpenAI-compatible adapter that batches, authenticates by header, and classifies failures for the embed job."""

import json
import math

import httpx
import pytest

from app.config import get_settings
from app.connectors.base import RateLimited
from app.connectors.egress import EgressDenied
from app.content.embeddings import (
    EmbeddingCredentialInvalid,
    EmbeddingRejected,
    EmbeddingUnavailable,
    LocalEmbedder,
    OpenAICompatibleEmbedder,
    get_embedder,
    to_pgvector,
)


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_the_local_embedder_is_deterministic_normalised_and_lexically_sensible():
    e = LocalEmbedder(dimensions=256)
    login, sso, billing = e.embed(
        ["Users log in with single sign-on", "single sign-on login for users", "Invoices are charged monthly"]
    )
    assert e.embed(["Users log in with single sign-on"])[0] == login  # deterministic
    assert len(login) == 256 and math.isclose(math.sqrt(sum(v * v for v in login)), 1.0)
    assert cosine(login, sso) > cosine(login, billing)
    assert e.embed([""])[0] == [0.0] * 256  # nothing to hash: a zero vector, never NaN
    assert to_pgvector([0.5, -1.0, 1e-9]) == "[0.5,-1,1e-09]"


def adapter(handler, **kw):
    e = OpenAICompatibleEmbedder(
        "voyage-3.5",
        3,
        "https://api.voyageai.com/v1",
        "sk-test",
        transport=httpx.MockTransport(handler),
        allowed_hosts="api.voyageai.com",
        **kw,
    )
    e._http._sleep = lambda s: None
    return e


def test_the_adapter_batches_authenticates_and_orders_by_index():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        data = [{"index": i, "embedding": [float(i), 0.0, 1.0]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": list(reversed(data))})

    e = adapter(handler, input_type=True)
    assert e.embed(["a", "b"], "query") == [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]]
    [request] = seen
    assert request.url == "https://api.voyageai.com/v1/embeddings"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {"model": "voyage-3.5", "input": ["a", "b"], "input_type": "query"}
    adapter(handler).embed(["a"])  # input_type off by default: strict APIs reject unknown fields
    assert "input_type" not in json.loads(seen[-1].content)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, headers={"Retry-After": "7"}), RateLimited),
        (httpx.Response(503), EmbeddingUnavailable),
        (httpx.Response(401), EmbeddingCredentialInvalid),
        (httpx.Response(400, json={"error": "input too long"}), EmbeddingRejected),
        (httpx.Response(200, json={"data": [{"embedding": [1.0, 2.0]}]}), EmbeddingRejected),  # 2 dims, not 3
        (httpx.Response(200, json={"unexpected": True}), EmbeddingRejected),
    ],
)
def test_failures_are_classified_for_the_job(response, error):
    with pytest.raises(error) as exc:
        adapter(lambda request: response).embed(["a"])
    if error is RateLimited:
        assert exc.value.retry_after == 7
    assert "sk-test" not in str(exc.value)  # the key never appears in an error


def test_the_base_url_must_pass_the_embedding_allowlist(monkeypatch):
    with pytest.raises(EgressDenied):
        OpenAICompatibleEmbedder(
            "m", 3, "https://evil.example.com/v1", None, allowed_hosts="api.voyageai.com"
        )
    monkeypatch.setenv("INPROOM_EMBEDDING_PROVIDER", "openai-compatible")
    get_settings.cache_clear()
    get_embedder.cache_clear()
    with pytest.raises(EmbeddingUnavailable):  # no base URL configured: an actionable, retryable error
        get_embedder()


def test_provider_vectors_are_validated_before_storage():
    """INPROOM-208: valid responses map onto the inputs; unsuitable ones are rejected, never stored."""
    from app.content.embeddings import EmbeddingRejected, vectors_from

    a, b = {"embedding": [1, 0]}, {"embedding": [0, 2.5]}
    assert vectors_from([a, b], 2, 2) == [[1.0, 0.0], [0.0, 2.5]]  # no indexes: input order
    shuffled = [b | {"index": 1}, a | {"index": 0}]
    assert vectors_from(shuffled, 2, 2) == [[1.0, 0.0], [0.0, 2.5]]  # indexes: reordered
    for data in (
        [a | {"index": 0}, b],  # some indexed, some not
        [a | {"index": 1}, b | {"index": 2}],  # not 0..n-1
        [a | {"index": True}, b | {"index": 0}],  # a bool is not an index
        [{"embedding": [True, 0]}, b],  # nor a number
        [{"embedding": "1,0"}, b],
        [{"vector": [1, 0]}, b],
        [a],  # one vector for two inputs
    ):
        with pytest.raises(EmbeddingRejected):
            vectors_from(data, 2, 2)
