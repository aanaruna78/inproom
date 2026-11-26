"""The embedding gateway (INPROOM-204 / story INPROOM-32, ADR-006 provider-neutral models).

One interface, `Embedder.embed(texts, purpose) -> vectors`, with two providers chosen by configuration:
- `local`: deterministic signed feature hashing of words and word pairs into `dimensions`, L2-normalised. No
  network, so it suits development, CI and air-gapped installs. Its quality is lexical, not semantic.
- `openai-compatible`: POST `{base_url}/embeddings` `{"model", "input": [...]}`, reading `data[].embedding`.
  It covers hosted APIs (Voyage AI, OpenAI) and self-hosted servers (Ollama, vLLM). The base URL must pass the
  embedding egress allowlist; the API key is a deployment secret sent only in the Authorization header.

Errors are classified for the embed job:
- EmbeddingUnavailable (timeouts, 5xx, transport errors) and RateLimited (429): transient, so the job is
  retried with backoff and nothing is recorded against the chunks;
- EmbeddingRejected (other 4xx, a malformed response, wrong dimensions): permanent for those inputs. The job
  isolates the offending chunk and records it as failed, with the reason.
"""

import hashlib
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

import httpx

from app.config import get_settings
from app.connectors.base import CredentialInvalid, ProviderError, ProviderUnavailable, RateLimited
from app.connectors.egress import validate_base_url
from app.connectors.http import ProviderHttp

Purpose = Literal["document", "query"]


class EmbeddingUnavailable(ProviderUnavailable):
    code = "EMBEDDING_UNAVAILABLE"


class EmbeddingCredentialInvalid(EmbeddingUnavailable):
    code = "EMBEDDING_CREDENTIAL_INVALID"


class EmbeddingRejected(ProviderError):
    code = "EMBEDDING_REJECTED"


class Embedder(Protocol):
    model: str
    version: str
    dimensions: int
    batch_size: int

    def embed(self, texts: list[str], purpose: Purpose = "document") -> list[list[float]]: ...


_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class LocalEmbedder:
    model: str = "local-hash-v1"
    version: str = "1"
    dimensions: int = 256
    batch_size: int = 64

    def _vector(self, text: str) -> list[float]:
        words = _WORD.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
        counts: dict[str, int] = {}
        for f in features:
            counts[f] = counts.get(f, 0) + 1
        vec = [0.0] * self.dimensions
        for feature, n in counts.items():
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign * (1.0 + math.log(n))
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed(self, texts: list[str], purpose: Purpose = "document") -> list[list[float]]:
        return [self._vector(t) for t in texts]


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        model: str,
        dimensions: int,
        base_url: str,
        api_key: str | None,
        *,
        batch_size: int = 64,
        timeout: float = 30.0,
        input_type: bool = False,
        allowed_hosts: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.version = model  # hosted models are versioned by name (for example voyage-3.5)
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.input_type = input_type
        base = validate_base_url(base_url, allowed_hosts, "Embedding provider")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = ProviderHttp("embedding provider", base, headers, transport=transport, max_attempts=2)
        self._http._client.timeout = httpx.Timeout(timeout, connect=5.0)

    def embed(self, texts: list[str], purpose: Purpose = "document") -> list[list[float]]:
        body: dict = {"model": self.model, "input": texts}
        if self.input_type:
            body["input_type"] = purpose
        try:
            response = self._http.request("POST", "/embeddings", json=body)
        except (RateLimited, EmbeddingUnavailable):
            raise
        except ProviderUnavailable as exc:
            raise EmbeddingUnavailable(str(exc)) from exc
        except CredentialInvalid as exc:
            raise EmbeddingCredentialInvalid("the embedding provider rejected the API key") from exc
        except ProviderError as exc:
            raise EmbeddingRejected(str(exc)) from exc
        try:
            data = sorted(response.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [[float(x) for x in d["embedding"]] for d in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingRejected("the embedding provider returned a malformed response") from exc
        if len(vectors) != len(texts):
            raise EmbeddingRejected(f"expected {len(texts)} vectors, got {len(vectors)}")
        if any(len(v) != self.dimensions for v in vectors):
            raise EmbeddingRejected(
                f"the model returned {len(vectors[0])} dimensions; configured {self.dimensions}"
            )
        return vectors


@lru_cache
def get_embedder() -> Embedder:
    """The active embedder, from configuration. Cleared with the settings cache in tests."""
    s = get_settings()
    if s.embedding_provider == "local":
        return LocalEmbedder(
            model=s.embedding_model, dimensions=s.embedding_dimensions, batch_size=s.embedding_batch_size
        )
    if s.embedding_provider == "openai-compatible":
        if not s.embedding_base_url:
            raise EmbeddingUnavailable("INPROOM_EMBEDDING_BASE_URL is not configured")
        return OpenAICompatibleEmbedder(
            s.embedding_model,
            s.embedding_dimensions,
            s.embedding_base_url,
            s.embedding_api_key,
            batch_size=s.embedding_batch_size,
            timeout=s.embedding_timeout_seconds,
            input_type=s.embedding_input_type,
            allowed_hosts=s.embedding_allowed_hosts,
        )
    raise EmbeddingUnavailable(f"unknown embedding provider {s.embedding_provider!r}")


def to_pgvector(vector: list[float]) -> str:
    """pgvector's text form: '[0.1,0.2,...]' (bound as text and cast with ::vector)."""
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"
