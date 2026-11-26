"""The embed job (INPROOM-204 / story INPROOM-32): one vector per chunk of visible current content, per model.

Queued per project (coalesced) whenever the derive job projects new chunks, and by a reindex. Each run:
1. selects distinct chunks (by sha256) of visible current revisions that have no vector for the active model
   and no recorded permanent failure for it, at most JOB_LIMIT;
2. embeds them in provider batches and commits each batch on its own: `ON CONFLICT DO NOTHING` on (tenant,
   model, sha256), so a retried or concurrent run never duplicates and progress survives an outage;
3. requeues itself while work remains.
Rate limits and outages propagate to the worker (retry with backoff; nothing recorded against the chunks). A
rejected batch is retried one input at a time to isolate the offending chunk, which is recorded in
chunk_embedding_failures with the provider's reason; the rest are embedded.
"""

import logging
import re
import uuid
from typing import Any

from sqlalchemy import Connection, and_, delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.content.embeddings import Embedder, EmbeddingRejected, get_embedder, to_pgvector
from app.content.visibility import visible
from app.db import get_engine, tenant_session
from app.db_schema import (
    chunk_embedding_failures,
    chunk_embeddings,
    knowledge_chunks,
    projects,
    source_objects,
)
from app.sync import queue
from app.sync.worker import JobContext, SkipJob, handler

log = logging.getLogger(__name__)
JOB_LIMIT = 512
_INDEXED: set[tuple[str, int]] = set()
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")


def queue_embed(conn: Connection, tenant: uuid.UUID, project_id: uuid.UUID, reason: str = "schedule") -> None:
    queue.enqueue(conn, tenant, project_id, "embed", project_id, reason)


def ensure_index(model: str, dimensions: int) -> bool:
    """Create the model's partial HNSW cosine index if missing (idempotent, once per process). Needs the
    table owner: where the runtime login is not the owner, the index is created at deploy time instead
    (docs/operations/vector-index.md) and search still works meanwhile by exact scan."""
    key = (model, dimensions)
    if key in _INDEXED:
        return True
    if not _SAFE_MODEL.match(model) or not 1 <= dimensions <= 2000:
        log.warning("embedding model %r / %s dimensions cannot be indexed by HNSW", model, dimensions)
        return False
    name = "ix_chunk_embeddings_" + re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")[:40]
    statement = (
        f'CREATE INDEX IF NOT EXISTS "{name}" ON chunk_embeddings '
        f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) WHERE model = '{model}'"
    )
    try:
        with get_engine().begin() as conn:
            conn.execute(text(statement))
    except Exception:  # noqa: BLE001 - an index is an optimisation; search falls back to an exact scan
        log.warning(
            "could not create the vector index for %s (create it at deploy time)", model, exc_info=True
        )
        return False
    _INDEXED.add(key)
    return True


def embedded_text(context: str, body: str) -> str:
    """Exactly what chunk sha256 covers (app.content.chunking._chunk)."""
    return f"{context}\n\n{body}" if context else body


def pending(conn: Connection, project_id: uuid.UUID, model: str, limit: int) -> list[dict[str, Any]]:
    k, o, e, f = knowledge_chunks, source_objects, chunk_embeddings, chunk_embedding_failures
    stmt = (
        select(k.c.sha256, k.c.context, k.c.text)
        .distinct(k.c.sha256)
        .join(o, and_(o.c.id == k.c.object_id, o.c.current_revision_id == k.c.revision_id))
        .where(
            k.c.project_id == project_id,
            visible(conn, project_id),
            ~exists().where(e.c.tenant_id == k.c.tenant_id, e.c.model == model, e.c.sha256 == k.c.sha256),
            ~exists().where(f.c.project_id == k.c.project_id, f.c.model == model, f.c.sha256 == k.c.sha256),
        )
        .order_by(k.c.sha256)
        .limit(limit)
    )
    return [dict(r) for r in conn.execute(stmt).mappings()]


def _store(tenant: uuid.UUID, embedder: Embedder, items: list[dict], vectors: list[list[float]]) -> int:
    with tenant_session(tenant) as conn:
        inserted = 0
        for item, vector in zip(items, vectors, strict=True):
            inserted += conn.execute(
                text(
                    "INSERT INTO chunk_embeddings "
                    "(tenant_id, model, model_version, dimensions, sha256, embedding) "
                    "VALUES (:t, :m, :v, :d, :s, CAST(:e AS vector)) "
                    "ON CONFLICT (tenant_id, model, sha256) DO NOTHING"
                ),
                {
                    "t": tenant,
                    "m": embedder.model,
                    "v": embedder.version,
                    "d": embedder.dimensions,
                    "s": item["sha256"],
                    "e": to_pgvector(vector),
                },
            ).rowcount
        conn.execute(
            delete(chunk_embedding_failures).where(
                chunk_embedding_failures.c.model == embedder.model,
                chunk_embedding_failures.c.sha256.in_([i["sha256"] for i in items]),
            )
        )
    return inserted


def _reject(tenant, project_id, model: str, item: dict, exc: EmbeddingRejected) -> None:
    f = chunk_embedding_failures
    with tenant_session(tenant) as conn:
        conn.execute(
            pg_insert(f)
            .values(
                tenant_id=tenant,
                project_id=project_id,
                model=model,
                sha256=item["sha256"],
                code=exc.code,
                detail=str(exc)[:500],
            )
            .on_conflict_do_update(
                constraint="uq_chunk_embedding_failures_key",
                set_={"attempts": f.c.attempts + 1, "detail": str(exc)[:500], "last_at": func.now()},
            )
        )


def run(tenant: uuid.UUID, project_id: uuid.UUID, embedder: Embedder) -> dict[str, Any]:
    with tenant_session(tenant) as conn:
        items = pending(conn, project_id, embedder.model, JOB_LIMIT + 1)
    more, items = len(items) > JOB_LIMIT, items[:JOB_LIMIT]
    counts: dict[str, Any] = {"model": embedder.model, "embedded": 0, "failed": 0}
    if items:
        ensure_index(embedder.model, embedder.dimensions)
    for start in range(0, len(items), embedder.batch_size):
        batch = items[start : start + embedder.batch_size]
        texts = [embedded_text(i["context"], i["text"]) for i in batch]
        try:
            counts["embedded"] += _store(tenant, embedder, batch, embedder.embed(texts, "document"))
        except EmbeddingRejected:
            for item, single in zip(batch, texts, strict=True):  # isolate the input the provider rejects
                try:
                    counts["embedded"] += _store(
                        tenant, embedder, [item], embedder.embed([single], "document")
                    )
                except EmbeddingRejected as exc:
                    _reject(tenant, project_id, embedder.model, item, exc)
                    counts["failed"] += 1
    counts["more"] = more
    return counts


@handler("embed")
def embed(ctx: JobContext) -> dict[str, Any]:
    tenant, project_id = ctx.tenant, ctx.job["project_id"]
    with tenant_session(tenant) as conn:
        status = conn.execute(select(projects.c.status).where(projects.c.id == project_id)).scalar()
    if status in (None, "ARCHIVED"):
        raise SkipJob("project is archived or gone")
    result = run(tenant, project_id, get_embedder())  # outages and rate limits: the worker retries
    with tenant_session(tenant) as conn:
        if result["more"]:
            queue_embed(conn, tenant, project_id, ctx.job["reason"])
        queue.succeed(conn, ctx.job, result)
    return result
