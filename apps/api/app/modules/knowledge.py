"""Knowledge change feed API (INPROOM-186 / story INPROOM-31).

`GET /v1/projects/{id}/knowledge/changes?after=&limit=` returns what became unservable (`invalidated`),
servable again (`restored`) or got new content (`refreshed`), in order, from the cursor `after` (a seq).
Entries carry identifiers only: consumers that hold copies (indexes, caches) drop or rebuild by them. See
app/content/feed.py for the invalidation contract.
"""

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, and_, func, select

from app import audit
from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.content import feed
from app.db import tenant_session
from app.db_schema import (
    code_snapshots,
    derivatives,
    knowledge_changes,
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_objects,
    sync_jobs,
)
from app.modules.sync_status import SyncJob

router = APIRouter(tags=["knowledge"])
log = logging.getLogger(__name__)
PROBLEM = {"description": "problem+json"}
MAX_CHANGES = 500


class Change(BaseModel):
    seq: int
    at: datetime
    change: str  # invalidated | restored | refreshed
    scope: str  # item | source
    kind: str
    source_id: uuid.UUID
    external_id: str | None  # item scope only
    branch: str | None  # git source scope (a branch deselected or selected again)
    reason: str
    revision_seq: int | None  # refreshed: the new revision; PURGED: the purged revision


class ChangePage(BaseModel):
    items: list[Change]
    head: int  # the project's latest seq
    next: int | None  # pass as ?after= to continue; None when caught up


@router.get(
    "/v1/projects/{project_id}/knowledge/changes",
    response_model=ChangePage,
    summary="The project's ordered, gap-free feed of invalidated, restored and refreshed knowledge",
)
def list_changes(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    after: Annotated[int, Query(ge=0, description="Return entries with seq greater than this")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_CHANGES)] = 100,
) -> ChangePage:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows, head = feed.changes(conn, project_id, after, limit)
    items = [
        Change(
            seq=r["seq"],
            at=r["at"],
            change=r["change"],
            scope=r["scope"],
            kind=r["source_kind"],
            source_id=r["source_id"],
            external_id=r["external_id"],
            branch=r["branch"],
            reason=r["reason"],
            revision_seq=r["revision_seq"],
        )
        for r in rows
    ]
    last = items[-1].seq if items else after
    return ChangePage(items=items, head=head, next=last if last < head else None)


# ---------------------------------------------------------------- refresh and status (INPROOM-189)

SOURCES = {
    "confluence": project_confluence_sources,
    "jira": project_jira_sources,
    "git": project_repositories,
}


class RefreshRequest(BaseModel):
    scope: Literal["project", "source"] = "project"
    kind: Literal["confluence", "jira", "git"] | None = None
    source_id: uuid.UUID | None = None
    reason: str = Field(min_length=3, max_length=300)


class RefreshResult(BaseModel):
    queued: list[SyncJob]  # full reconciles, coalesced with jobs already queued for the same source
    skipped: list[dict[str, str]]  # paused sources: {kind, source_id, reason}


def _project_sources(conn: Connection, project_id: uuid.UUID) -> list[dict]:
    out = []
    for kind, table in SOURCES.items():
        name = (
            table.c.full_name
            if kind == "git"
            else (table.c.jira_key if kind == "jira" else table.c.space_key)
        )
        for r in conn.execute(
            select(table.c.id, table.c.status, name.label("name")).where(table.c.project_id == project_id)
        ).mappings():
            out.append({"kind": kind, "source_id": r["id"], "status": r["status"], "name": r["name"]})
    return sorted(out, key=lambda x: (x["kind"], x["name"] or ""))


@router.post(
    "/v1/projects/{project_id}/knowledge/refresh",
    status_code=202,
    response_model=RefreshResult,
    summary="Reconcile the project's sources (or one) in full now: deletions and permission changes included",
    responses={404: PROBLEM, 409: PROBLEM},
)
def refresh(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[RefreshRequest, Body()],
) -> JSONResponse:
    from app.sync import queue

    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        targets = _project_sources(conn, project_id)
        if payload.scope == "source":
            if payload.kind is None or payload.source_id is None:
                raise ApiError(
                    422, "VALIDATION_FAILED", "Invalid request", "A source refresh needs kind and source_id"
                )
            targets = [t for t in targets if (t["kind"], t["source_id"]) == (payload.kind, payload.source_id)]
            if not targets:
                raise ApiError(
                    404, "SOURCE_NOT_FOUND", "Not found", "This source is not selected for the project"
                )
        queued, skipped = [], []
        for t in targets:
            if t["status"] != "ACTIVE":
                skipped.append(
                    {"kind": t["kind"], "source_id": str(t["source_id"]), "reason": "SOURCE_PAUSED"}
                )
                continue
            job = queue.enqueue(
                conn,
                principal.tenant_id,
                project_id,
                t["kind"],
                t["source_id"],
                "manual",
                mode="full",
                hints={"requested_by": principal.user_id, "knowledge_refresh": payload.reason},
            )
            queued.append(SyncJob.model_validate(job))
        audit.record(
            conn,
            principal,
            "knowledge.refresh",
            "project",
            str(project_id),
            trace_id=getattr(request.state, "trace_id", None),
            payload={
                "scope": payload.scope,
                "reason": payload.reason,
                "jobs": [str(j.id) for j in queued],
                "skipped": skipped,
            },
            project_id=project_id,
        )
    body = RefreshResult(queued=queued, skipped=skipped)
    return JSONResponse(body.model_dump(mode="json"), status_code=202)


class SourceStatus(BaseModel):
    kind: str
    source_id: uuid.UUID
    name: str | None
    status: str
    last_change_seq: int | None  # the latest feed entry about this source or its items
    last_change_at: datetime | None
    deletions_verified_at: datetime | None  # the last reconcile that saw the source's whole item set
    pending_derivatives: int  # derived outputs of current content still being built


class KnowledgeStatus(BaseModel):
    project_id: uuid.UUID
    head: int
    sources: list[SourceStatus]


def _verified_at(conn: Connection, kind: str, source_id: uuid.UUID) -> datetime | None:
    """When deletions were last verified: Confluence lists every page on every sync; Jira only on a full
    reconcile; git on every COMPLETE snapshot (any branch)."""
    if kind == "git":
        return conn.execute(
            select(func.max(code_snapshots.c.finished_at)).where(
                code_snapshots.c.source_id == source_id, code_snapshots.c.status == "COMPLETE"
            )
        ).scalar()
    stmt = select(func.max(sync_jobs.c.finished_at)).where(
        sync_jobs.c.source_kind == kind, sync_jobs.c.source_id == source_id, sync_jobs.c.status == "SUCCEEDED"
    )
    if kind == "jira":
        stmt = stmt.where(sync_jobs.c.result["full"].as_boolean().is_(True))
    job = conn.execute(stmt).scalar()
    if kind == "confluence":  # syncs run through the API (selection, sync now) record no job, only this
        api = conn.execute(
            select(project_confluence_sources.c.last_synced_at).where(
                project_confluence_sources.c.id == source_id
            )
        ).scalar()
        return max((t for t in (job, api) if t is not None), default=None)
    return job


@router.get(
    "/v1/projects/{project_id}/knowledge/status",
    response_model=KnowledgeStatus,
    summary="How current the project's knowledge is: feed head, per-source last change and deletion checks",
)
def status(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> KnowledgeStatus:
    k, o, d = knowledge_changes, source_objects, derivatives
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        head = conn.execute(
            select(func.coalesce(func.max(k.c.seq), 0)).where(k.c.project_id == project_id)
        ).scalar()
        last = {
            (r.source_kind, r.source_id): (r.seq, r.at)
            for r in conn.execute(
                select(
                    k.c.source_kind,
                    k.c.source_id,
                    func.max(k.c.seq).label("seq"),
                    func.max(k.c.at).label("at"),
                )
                .where(k.c.project_id == project_id)
                .group_by(k.c.source_kind, k.c.source_id)
            )
        }
        pending = {
            (r.source_kind, r.source_id): r.n
            for r in conn.execute(
                select(o.c.source_kind, o.c.source_id, func.count().label("n"))
                .select_from(d.join(o, o.c.current_revision_id == d.c.revision_id))
                .where(d.c.project_id == project_id, d.c.status == "PENDING", o.c.status == "CURRENT")
                .group_by(o.c.source_kind, o.c.source_id)
            )
        }
        sources = [
            SourceStatus(
                kind=t["kind"],
                source_id=t["source_id"],
                name=t["name"],
                status=t["status"],
                last_change_seq=last.get((t["kind"], t["source_id"]), (None, None))[0],
                last_change_at=last.get((t["kind"], t["source_id"]), (None, None))[1],
                deletions_verified_at=_verified_at(conn, t["kind"], t["source_id"]),
                pending_derivatives=pending.get((t["kind"], t["source_id"]), 0),
            )
            for t in _project_sources(conn, project_id)
        ]
    return KnowledgeStatus(project_id=project_id, head=head, sources=sources)


# ---------------------------------------------------------------- vector search (INPROOM-205)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)
    kinds: list[Literal["confluence", "jira", "git", "upload"]] | None = None
    source_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


class Citation(BaseModel):
    kind: str
    source_id: uuid.UUID
    external_id: str
    seq: int
    anchor: str | None


class SearchHit(BaseModel):
    citation: Citation  # resolves through POST /provenance/resolve
    title: str
    path: list[str]  # heading path (documents)
    context: str
    text: str
    page: int | None
    line_start: int | None  # source files
    line_end: int | None
    score: float  # cosine similarity in [-1, 1]; higher is closer
    retrieved_at: datetime  # when INPROOM fetched this revision
    provenance: dict  # freshness, trust, classification, derivation (INPROOM-184)


class SearchResult(BaseModel):
    model: str
    model_version: str
    dimensions: int
    items: list[SearchHit]


def _hit_fields(conn: Connection, row: dict) -> dict:
    """What every retrieval hit carries: citation, location, text and the cited revision's provenance."""
    from app.content import provenance
    from app.db_schema import source_revisions

    obj = dict(
        conn.execute(select(source_objects).where(source_objects.c.id == row["object_id"])).mappings().one()
    )
    rev = dict(
        conn.execute(select(source_revisions).where(source_revisions.c.id == row["revision_id"]))
        .mappings()
        .one()
    )
    return {
        "citation": _citation(row),
        "title": row["title"] or "",
        "path": list(row["path"] or []),
        "context": row["context"],
        "text": row["text"],
        "page": row["page"],
        "line_start": row["line_start"],
        "line_end": row["line_end"],
        "retrieved_at": row["retrieved_at"],
        "provenance": provenance.summary(provenance.build(conn, obj, rev, None)),
    }


@router.post(
    "/v1/projects/{project_id}/knowledge/search",
    response_model=SearchResult,
    summary="Semantic search over the project's chunks: visible current content only, active model only",
    responses={404: PROBLEM, 503: PROBLEM},
)
def search(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[SearchRequest, Body()],
) -> SearchResult:
    from app.connectors.base import RateLimited
    from app.content.embeddings import EmbeddingRejected, EmbeddingUnavailable, get_embedder
    from app.content.retrieval import scope, semantic_candidates

    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
    try:  # outside any transaction: the provider call can take seconds
        embedder = get_embedder()
        vector = embedder.embed([payload.query], "query")[0]
    except (RateLimited, EmbeddingUnavailable, EmbeddingRejected) as exc:
        raise _embedding_unavailable(exc) from exc
    dims = int(embedder.dimensions)
    with tenant_session(principal.tenant_id) as conn:
        base = scope(conn, project_id, kinds=payload.kinds, source_ids=payload.source_ids)
        rows = semantic_candidates(conn, base, embedder.model, dims, vector, payload.top_k)
        items = [SearchHit(**_hit_fields(conn, row), score=row["similarity"]) for row in rows]
    return SearchResult(model=embedder.model, model_version=embedder.version, dimensions=dims, items=items)


# ---------------------------------------------------------------- hybrid retrieval (INPROOM-33)


class RetrieveRequest(BaseModel):
    # words, "phrases" and -exclusions (see app.content.retrieval)
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["hybrid", "keyword", "semantic"] = "hybrid"
    top_k: int = Field(default=10, ge=1, le=50)
    max_per_document: int = Field(default=3, ge=1, le=50)  # chunks of one item at most
    # freshness: items INPROOM saw change after this time, or within the last N days (the stricter applies)
    changed_after: datetime | None = None
    max_age_days: int | None = Field(default=None, ge=1, le=3650)
    exclude_stale: bool = False  # drop items whose provenance freshness is stale (e.g. their source's sync)
    include_unsupported: bool = False  # also return hits that are not evidence (marked supported=false)
    kinds: list[Literal["confluence", "jira", "git", "upload"]] | None = None
    source_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)


class KeywordScore(BaseModel):
    rank: int  # 1-based rank among the keyword candidates
    score: float  # ts_rank_cd (cover density); comparable within one query only
    terms: list[str]  # the query words and phrases this chunk matched


class SemanticScore(BaseModel):
    rank: int  # 1-based rank among the semantic candidates
    similarity: float  # cosine similarity under the active model, in [-1, 1]


class Explanation(BaseModel):
    keyword: KeywordScore | None  # None: not among the keyword candidates
    semantic: SemanticScore | None  # None: not among the semantic candidates (or semantic was degraded)
    rrf: float  # the fused score: sum of 1 / (k + rank) over the lists it is in; the ranking key
    supported: bool  # evidence: matched a query term, or similarity reached INPROOM_RETRIEVAL_MIN_SIMILARITY


class Location(BaseModel):
    """Another place the same text (same sha256) appears; the hit is returned once."""

    citation: Citation
    title: str


class RetrieveHit(BaseModel):
    citation: Citation  # resolves through POST /provenance/resolve
    title: str
    path: list[str]
    context: str
    text: str
    page: int | None
    line_start: int | None
    line_end: int | None
    retrieved_at: datetime
    provenance: dict
    also_in: list[Location]
    explanation: Explanation


class Evidence(BaseModel):
    status: Literal["sufficient", "insufficient"]  # insufficient: do not answer from these results
    reason: Literal["NO_CONTENT", "ALL_FILTERED", "NO_MATCH", "LOW_RELEVANCE"] | None
    detail: str
    supported: int  # supported hits returned


class RetrieveResult(BaseModel):
    mode: str
    evidence: Evidence
    model: str | None  # the embedding model of the semantic candidates; None when none were used
    degraded: list[str]  # parts that could not run, e.g. ["semantic"] during an embedding outage
    query_terms: list[str]  # how the query was understood: the words and phrases searched for
    excluded_terms: list[str]
    ignored_terms: list[str]  # stop words only: they would match almost everything
    items: list[RetrieveHit]


def _embedding_unavailable(exc: Exception) -> ApiError:
    """The problem response for a failed query embedding: a rejection of this query is the caller's to fix
    (422, nothing to retry); a rate limit or an outage is transient (503 with Retry-After)."""
    from app.connectors.base import RateLimited
    from app.content.embeddings import EmbeddingRejected

    if isinstance(exc, EmbeddingRejected):  # INPROOM-209/219: e.g. over the input limit, unsupported input
        return ApiError(
            422,
            "EMBEDDING_REJECTED",
            "The embedding provider rejected the query",
            f"{str(exc)[:300]}. Shorten or rephrase the query; keyword retrieval does not need embeddings",
        )
    if isinstance(exc, RateLimited):
        return ApiError(
            503,
            "EMBEDDING_UNAVAILABLE",
            "Embedding provider busy",
            "Retry shortly",
            headers={"Retry-After": str(exc.retry_after)},
        )
    return ApiError(
        503,
        "EMBEDDING_UNAVAILABLE",
        "Embedding provider unavailable",
        str(exc)[:300],
        headers={"Retry-After": "30"},
    )


def _citation(row: dict) -> Citation:
    return Citation(
        kind=row["source_kind"],
        source_id=row["source_id"],
        external_id=row["external_id"],
        seq=row["seq"],
        anchor=row["anchor"],
    )


@router.post(
    "/v1/projects/{project_id}/knowledge/retrieve",
    response_model=RetrieveResult,
    summary="Hybrid retrieval: keyword and semantic candidates fused by rank, de-duplicated and explained",
    responses={404: PROBLEM, 422: PROBLEM, 503: PROBLEM},
)
def retrieve(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[RetrieveRequest, Body()],
) -> RetrieveResult:
    """Visible current content only: every candidate query applies the visibility rule. In `hybrid` mode an
    embedding outage degrades to keyword candidates (`degraded: ["semantic"]`); `semantic` mode gives 503."""
    from app.config import get_settings
    from app.connectors.base import RateLimited
    from app.content import provenance
    from app.content.embeddings import EmbeddingRejected, EmbeddingUnavailable, get_embedder
    from app.content.retrieval import (
        evidence,
        fuse,
        keyword_candidates,
        parse_query,
        scope,
        semantic_candidates,
        supported,
        without_stop_words,
    )

    started = time.monotonic()
    parsed = parse_query(payload.query)
    if not parsed.units:
        raise ApiError(
            422,
            "QUERY_HAS_NO_TERMS",
            "The query has no searchable terms",
            "Add at least one word or phrase that is not excluded",
        )
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
    config = get_settings()
    embedder, vector, degraded = None, None, []
    if payload.mode != "keyword":
        try:  # outside any transaction: the provider call can take seconds
            embedder = get_embedder()
            vector = embedder.embed([" ".join(parsed.units)], "query")[0]
        except (RateLimited, EmbeddingUnavailable, EmbeddingRejected) as exc:
            if payload.mode == "semantic":
                raise _embedding_unavailable(exc) from exc
            embedder, degraded = None, ["semantic"]  # hybrid: keyword candidates still answer
    limit = max(config.retrieval_candidates, payload.top_k)
    cutoffs = [payload.changed_after] if payload.changed_after else []
    if payload.max_age_days:
        cutoffs.append(datetime.now(UTC) - timedelta(days=payload.max_age_days))
    changed_after = max(cutoffs) if cutoffs else None
    with tenant_session(principal.tenant_id) as conn:
        parsed = without_stop_words(conn, parsed)
        stale = []
        if payload.exclude_stale:
            stale = [
                t["source_id"]
                for t in _project_sources(conn, project_id)
                if provenance.source_health(conn, t["kind"], t["source_id"])[0] in ("stale", "failing")
            ]
        unfiltered = scope(
            conn, project_id, kinds=payload.kinds, source_ids=payload.source_ids, excluded=parsed.excluded
        )

        counts: dict[str, int] = {}

        def candidates(base):
            keyword = (
                keyword_candidates(conn, base, parsed.units, limit) if payload.mode != "semantic" else []
            )
            semantic = (
                semantic_candidates(conn, base, embedder.model, embedder.dimensions, vector, limit)
                if embedder is not None
                else []
            )
            fused = fuse(
                keyword,
                semantic,
                k=config.retrieval_rrf_k,
                top_k=2 * limit,
                max_per_document=payload.max_per_document,
            )
            for row in fused:
                row["supported"] = supported(row, config.retrieval_min_similarity)
            counts.setdefault("keyword", len(keyword))  # the first (served) run's candidate counts
            counts.setdefault("semantic", len(semantic))
            return fused

        filtering = bool(changed_after or stale)
        base = (
            scope(
                conn,
                project_id,
                kinds=payload.kinds,
                source_ids=payload.source_ids,
                excluded=parsed.excluded,
                changed_after=changed_after,
                without_sources=stale,
            )
            if filtering
            else unfiltered
        )
        fused = candidates(base)
        items: list[RetrieveHit] = []
        for row in fused:
            if len(items) == payload.top_k:
                break
            if not (row["supported"] or payload.include_unsupported):
                continue
            fields = _hit_fields(conn, row)
            if payload.exclude_stale and fields["provenance"]["freshness"] == "stale":
                filtering = True
                continue  # an item-level stale signal (e.g. the provider changed it after retrieval)
            items.append(
                RetrieveHit(
                    **fields,
                    also_in=[Location(citation=_citation(o), title=o["title"] or "") for o in row["also_in"]],
                    explanation=Explanation(
                        keyword=KeywordScore(**row["keyword"] | {"score": round(row["keyword"]["score"], 6)})
                        if row["keyword"]
                        else None,
                        semantic=SemanticScore(**row["semantic"]) if row["semantic"] else None,
                        rrf=row["rrf"],
                        supported=row["supported"],
                    ),
                )
            )
        returned = sum(1 for i in items if i.explanation.supported)
        has_content = would_match = False
        if not returned:  # only an empty answer pays for the diagnosis
            has_content = conn.execute(unfiltered.limit(1)).first() is not None
            would_match = filtering and any(r["supported"] for r in candidates(unfiltered))
        verdict = evidence(
            returned,
            has_content=has_content,
            filtered_would_match=would_match,
            candidates=len(fused),
            degraded=degraded,
        )
    # telemetry (INPROOM-214): never the query text, which may be sensitive
    summary = {
        "mode": payload.mode,
        "model": embedder.model if embedder is not None else None,
        "degraded": degraded,
        "evidence": verdict["reason"] or "SUFFICIENT",
        "candidates": counts,
        "returned": len(items),
        "supported": returned,
        "filters": sorted(
            k for k in ("changed_after", "max_age_days", "kinds", "source_ids") if getattr(payload, k)
        )
        + (["exclude_stale"] if payload.exclude_stale else []),
        "ms": round((time.monotonic() - started) * 1000),
    }
    log.info("knowledge retrieve project=%s %s", project_id, json.dumps(summary))
    return RetrieveResult(
        mode=payload.mode,
        evidence=Evidence(**verdict, supported=returned),
        model=embedder.model if embedder is not None else None,
        degraded=degraded,
        query_terms=parsed.units,
        excluded_terms=parsed.excluded,
        ignored_terms=parsed.ignored,
        items=items,
    )


# ---------------------------------------------------------------- index status and reindex (INPROOM-206)


class ModelVectors(BaseModel):
    model: str
    vectors: int  # vectors of this model for the project's visible current chunks


class IndexStatus(BaseModel):
    project_id: uuid.UUID
    provider: str
    model: str  # the active model: search and the embed job use it
    model_version: str
    dimensions: int
    chunks: int  # distinct chunks (by text) of visible current content
    embedded: int  # ... with a vector for the active model
    failed: int  # ... the provider rejected for the active model (see /index/failures)
    pending: int  # ... still to embed (the embed job is queued or running)
    derivatives: dict[str, int]  # chunks@1 derivatives of current revisions by status
    models: list[ModelVectors]  # every model with vectors here: more than one while a model switch runs


def _visible_chunks(conn: Connection, project_id: uuid.UUID):
    from app.content.visibility import visible
    from app.db_schema import knowledge_chunks as k

    o = source_objects
    return (
        select(k.c.sha256)
        .distinct()
        .join(o, and_(o.c.id == k.c.object_id, o.c.current_revision_id == k.c.revision_id))
        .where(k.c.project_id == project_id, visible(conn, project_id))
        .subquery()
    )


@router.get(
    "/v1/projects/{project_id}/knowledge/index",
    response_model=IndexStatus,
    summary="The vector index: chunks, embedded, pending and failed for the active model; models present",
)
def index_status(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> IndexStatus:
    from app.config import get_settings
    from app.content.embeddings import get_embedder
    from app.db_schema import chunk_embedding_failures as f
    from app.db_schema import chunk_embeddings as e
    from app.db_schema import source_revisions as r

    embedder = get_embedder()
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        shas = _visible_chunks(conn, project_id)
        total = conn.execute(select(func.count()).select_from(shas)).scalar_one()
        by_model = {
            row.model: row.n
            for row in conn.execute(
                select(e.c.model, func.count().label("n"))
                .join(shas, shas.c.sha256 == e.c.sha256)
                .group_by(e.c.model)
            )
        }
        failed = conn.execute(
            select(func.count())
            .select_from(f)
            .join(shas, shas.c.sha256 == f.c.sha256)
            .where(f.c.project_id == project_id, f.c.model == embedder.model)
        ).scalar_one()
        derived = {
            row.status: row.n
            for row in conn.execute(
                select(derivatives.c.status, func.count().label("n"))
                .join(r, r.c.id == derivatives.c.revision_id)
                .join(source_objects, source_objects.c.current_revision_id == r.c.id)
                .where(derivatives.c.project_id == project_id, derivatives.c.producer == "chunks")
                .group_by(derivatives.c.status)
            )
        }
    embedded = by_model.get(embedder.model, 0)
    return IndexStatus(
        project_id=project_id,
        provider=get_settings().embedding_provider,
        model=embedder.model,
        model_version=embedder.version,
        dimensions=embedder.dimensions,
        chunks=total,
        embedded=embedded,
        failed=failed,
        pending=max(0, total - embedded - failed),
        derivatives=derived,
        models=[ModelVectors(model=m, vectors=n) for m, n in sorted(by_model.items())],
    )


class IndexFailure(BaseModel):
    citation: Citation  # one chunk with this text (identical text elsewhere fails the same way)
    model: str
    code: str
    detail: str
    attempts: int
    last_at: datetime


@router.get(
    "/v1/projects/{project_id}/knowledge/index/failures",
    response_model=list[IndexFailure],
    summary="Chunks the embedding provider rejected for the active model, with the reason",
)
def index_failures(
    request: Request, principal: CurrentPrincipal, project_id: uuid.UUID
) -> list[IndexFailure]:
    from app.content.embeddings import get_embedder
    from app.db_schema import chunk_embedding_failures as f
    from app.db_schema import knowledge_chunks as k

    model = get_embedder().model
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = conn.execute(
            select(f, k.c.source_kind, k.c.source_id, k.c.external_id, k.c.seq, k.c.anchor)
            .distinct(f.c.id)
            .join(k, and_(k.c.project_id == f.c.project_id, k.c.sha256 == f.c.sha256))
            .where(f.c.project_id == project_id, f.c.model == model)
            .order_by(f.c.id, k.c.id)
            .limit(500)
        ).mappings()
        return [
            IndexFailure(
                citation=Citation(
                    kind=x["source_kind"],
                    source_id=x["source_id"],
                    external_id=x["external_id"],
                    seq=x["seq"],
                    anchor=x["anchor"],
                ),
                model=x["model"],
                code=x["code"],
                detail=x["detail"],
                attempts=x["attempts"],
                last_at=x["last_at"],
            )
            for x in rows
        ]


class ReindexRequest(BaseModel):
    scope: Literal["embeddings", "chunks"]
    reason: str = Field(min_length=3, max_length=300)


class ReindexResult(BaseModel):
    scope: str
    model: str
    failures_cleared: int  # embeddings: rejected chunks get another attempt
    chunks_invalidated: int  # chunks: derivatives queued for a rebuild (then re-embedded)


@router.post(
    "/v1/projects/{project_id}/knowledge/reindex",
    status_code=202,
    response_model=ReindexResult,
    summary="Re-embed (after a model switch or a fixed failure) or rebuild the chunks; coalesced, audited",
    responses={404: PROBLEM, 409: PROBLEM},
)
def reindex(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[ReindexRequest, Body()],
) -> JSONResponse:
    from sqlalchemy import delete

    from app.content import derivatives as derived
    from app.content.embed import queue_embed
    from app.content.embeddings import get_embedder
    from app.db_schema import chunk_embedding_failures as f

    model = get_embedder().model
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        cleared = conn.execute(delete(f).where(f.c.project_id == project_id, f.c.model == model)).rowcount
        invalidated = 0
        if payload.scope == "chunks":
            targets = [(t["kind"], t["source_id"]) for t in _project_sources(conn, project_id)]
            for kind, source_id in [*targets, ("upload", project_id)]:
                done = derived.invalidate(
                    conn,
                    principal.tenant_id,
                    project_id,
                    kind=kind,
                    source_id=source_id,
                    external_id=None,
                    producer="chunks",
                    reason=payload.reason,
                )
                invalidated += done["invalidated"]
        queue_embed(conn, principal.tenant_id, project_id, "manual")  # coalesced with a queued run
        audit.record(
            conn,
            principal,
            "knowledge.reindex",
            "project",
            str(project_id),
            trace_id=getattr(request.state, "trace_id", None),
            payload={
                "scope": payload.scope,
                "reason": payload.reason,
                "model": model,
                "failures_cleared": cleared,
                "chunks_invalidated": invalidated,
            },
            project_id=project_id,
        )
    body = ReindexResult(
        scope=payload.scope, model=model, failures_cleared=cleared, chunks_invalidated=invalidated
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=202)
