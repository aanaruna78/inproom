"""Knowledge graph read API (INPROOM-217 / story INPROOM-34). See app/content/graph.py for the model.

Everything returned passes the visibility rule (app.content.visibility):
- a node is shown when at least one of its occurrences belongs to a visible item;
- an edge is shown when the item that stated it is visible AND it was extracted from that item's current
  revision, so evidence is never served from a superseded revision while the graph job catches up
  (INPROOM-221); evidence only ever comes from visible items;
- an edge whose endpoint exists but is hidden is hidden too, so a restricted item is never revealed through
  a link; an endpoint no item carries at all is an **unresolved reference** (shown, flagged).
Rows from several items stating the same edge are merged by natural key (from, kind, to): origin is
`validated` when any source field states it, confidence is the highest, evidence is combined. Curators'
reviews (INPROOM-218) set `status`: `confirmed`, `rejected` (hidden unless asked for) or `active`.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import Connection, func, or_, select, tuple_

from app.api_errors import ApiError
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.db import tenant_session
from app.db_schema import graph_edges, graph_nodes, graph_reviews, source_objects

router = APIRouter(tags=["graph"])
PROBLEM = {"description": "problem+json"}
MAX_EVIDENCE = 10  # per merged edge
LISTED = 50  # orphans and unresolved references listed in status


class Item(BaseModel):
    kind: str
    source_id: uuid.UUID
    external_id: str
    title: str


class Node(BaseModel):
    key: str  # natural key, e.g. jira:PAY-17
    type: str
    label: str
    attributes: dict
    items: list[Item]  # the item(s) this node is (two or more: the same entity from several items)
    asserted_by: int  # visible items that state this node (its own item(s), or the items naming it)
    degree: int  # visible edges touching it (rejected ones excluded)


class NodePage(BaseModel):
    items: list[Node]
    next: str | None  # pass as ?cursor= to continue


class Edge(BaseModel):
    id: int  # stable while the stating rows exist; used to review the edge
    from_key: str
    kind: str
    to_key: str
    origin: Literal["validated", "inferred", "manual"]
    confidence: float
    status: Literal["active", "confirmed", "rejected"]
    unresolved: bool  # an endpoint no item carries (e.g. a key of an unconnected Jira project)
    extractor: str
    evidence: list[dict]  # {citation, field?, value?, text?, excerpt?}; citations resolve via provenance
    reviewed: dict | None  # {action, reason, actor, at} when a curator reviewed it


class NodeDetail(BaseModel):
    node: Node
    outgoing: list[Edge]
    incoming: list[Edge]


class EdgePage(BaseModel):
    items: list[Edge]
    next: int | None


class Count(BaseModel):
    name: str
    count: int


class Unresolved(BaseModel):
    key: str
    references: int  # edges pointing at it


class GraphStatus(BaseModel):
    extractor: str
    pending: int  # items waiting for (re)extraction by the graph job
    nodes: int
    edges: int
    node_types: list[Count]
    edge_kinds: list[Count]
    origins: list[Count]  # validated, inferred, manual
    statuses: list[Count]  # active, confirmed, rejected
    orphans: int  # item nodes with no visible edge
    orphan_keys: list[str]  # the first LISTED
    unresolved: int
    unresolved_keys: list[Unresolved]  # the most referenced LISTED


# ---------------------------------------------------------------- shared reads


def _visible_ids(conn: Connection, project_id: uuid.UUID):
    from app.content.visibility import visible

    return select(source_objects.c.id).where(visible(conn, project_id))


def _current_visible(conn: Connection, project_id: uuid.UUID):
    """(object, current revision) of visible items. An edge row extracted from an older revision is evidence
    of content that is no longer current: not served until the graph job re-extracts (INPROOM-221)."""
    from app.content.visibility import visible

    o = source_objects
    return select(o.c.id, o.c.current_revision_id).where(visible(conn, project_id))


def _shown_keys(conn: Connection, project_id: uuid.UUID, keys: set[str]) -> tuple[set[str], set[str]]:
    """(keys some node occurrence carries, keys a visible occurrence carries) among `keys`."""
    if not keys:
        return set(), set()
    n = graph_nodes
    rows = conn.execute(
        select(n.c.key, func.bool_or(n.c.object_id.in_(_visible_ids(conn, project_id))))
        .where(n.c.project_id == project_id, n.c.key.in_(list(keys)))
        .group_by(n.c.key)
    ).all()
    return {r[0] for r in rows}, {r[0] for r in rows if r[1]}


def edges(
    conn: Connection,
    project_id: uuid.UUID,
    *,
    keys: list[str] | None = None,
    kind: str | None = None,
    include_rejected: bool = False,
) -> list[dict[str, Any]]:
    """Visible edges merged by natural key, best confidence first (then by id)."""
    e = graph_edges
    stmt = select(e).where(
        e.c.project_id == project_id,
        tuple_(e.c.object_id, e.c.revision_id).in_(_current_visible(conn, project_id)),
    )
    if keys is not None:
        stmt = stmt.where(or_(e.c.from_key.in_(keys), e.c.to_key.in_(keys)))
    if kind:
        stmt = stmt.where(e.c.kind == kind)
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in conn.execute(stmt.order_by(e.c.id)).mappings():
        natural = (row["from_key"], row["kind"], row["to_key"])
        m = merged.get(natural)
        if m is None:
            merged[natural] = m = {
                "id": row["id"],
                "from_key": row["from_key"],
                "kind": row["kind"],
                "to_key": row["to_key"],
                "origin": row["origin"],
                "confidence": row["confidence"],
                "extractor": row["extractor"],
                "evidence": [],
            }
        if row["origin"] == "validated":
            m["origin"] = "validated"
        m["confidence"] = max(m["confidence"], row["confidence"])
        m["evidence"] = (m["evidence"] + list(row["evidence"] or []))[:MAX_EVIDENCE]
    _drop_diverged_duplicates(conn, project_id, merged)
    reviews = _reviews(conn, project_id, merged.keys())
    for natural, review in reviews.items():  # manual edges (INPROOM-218) live only as reviews
        if review["action"] == "manual" and natural not in merged:
            if keys is not None and not ({natural[0], natural[2]} & set(keys)):
                continue
            if kind and natural[1] != kind:
                continue
            merged[natural] = {
                "id": -review["id"],  # negative: a manual edge's id is its review's
                "from_key": natural[0],
                "kind": natural[1],
                "to_key": natural[2],
                "origin": "manual",
                "confidence": 1.0,
                "extractor": "manual",
                "evidence": [{"field": "manual", "reason": review["reason"], "actor": review["actor"]}],
            }
    endpoints = {k for m in merged.values() for k in (m["from_key"], m["to_key"])}
    present, shown = _shown_keys(conn, project_id, endpoints)
    out = []
    for natural, m in merged.items():
        ends = (m["from_key"], m["to_key"])
        if any(k in present and k not in shown for k in ends):
            continue  # an endpoint exists but is hidden: so is the edge
        review = reviews.get(natural)
        action = review["action"] if review else None
        status = {"confirm": "confirmed", "reject": "rejected", "manual": "confirmed"}.get(action, "active")
        if status == "rejected" and not include_rejected:
            continue
        m["status"] = status
        m["unresolved"] = any(k not in present for k in ends)
        m["reviewed"] = (
            {k: review[k] for k in ("action", "reason", "actor", "at")} if review is not None else None
        )
        out.append(m)
    out.sort(key=lambda m: (-m["confidence"], abs(m["id"])))
    return out


def _drop_diverged_duplicates(conn: Connection, project_id: uuid.UUID, merged: dict) -> None:
    """INPROOM-222: a `duplicates` edge holds only while both twins' CURRENT content still has the sha256 its
    evidence names. Until the graph job re-checks a twin, a pair whose content diverged is not served."""
    pairs = {k: m for k, m in merged.items() if k[1] == "duplicates"}
    if not pairs:
        return
    from app.db_schema import source_revisions

    n, o, r = graph_nodes, source_objects, source_revisions
    keys = {k for natural in pairs for k in (natural[0], natural[2])}
    current: dict[str, set[str]] = {}
    for key, sha in conn.execute(
        select(n.c.key, r.c.content_sha256)
        .join(o, o.c.id == n.c.object_id)
        .join(r, r.c.id == o.c.current_revision_id)
        .where(n.c.project_id == project_id, n.c.role == "item", n.c.key.in_(list(keys)))
    ):
        current.setdefault(key, set()).add(sha)
    for natural, m in pairs.items():
        shas = {ev.get("value") for ev in m["evidence"] if ev.get("field") == "content_sha256"}
        if not any(sha in current.get(natural[0], ()) and sha in current.get(natural[2], ()) for sha in shas):
            del merged[natural]


def _reviews(conn: Connection, project_id: uuid.UUID, naturals) -> dict[tuple[str, str, str], dict]:
    r = graph_reviews
    rows = conn.execute(select(r).where(r.c.project_id == project_id)).mappings()
    wanted = set(naturals)
    return {
        (x["from_key"], x["kind"], x["to_key"]): dict(x)
        for x in rows
        if (x["from_key"], x["kind"], x["to_key"]) in wanted or x["action"] == "manual"
    }


def _nodes(conn: Connection, project_id: uuid.UUID, keys: list[str]) -> list[dict[str, Any]]:
    """Visible nodes for `keys` (in that order), with their items and the count of asserting items."""
    if not keys:
        return []
    n, o = graph_nodes, source_objects
    rows = conn.execute(
        select(n, o.c.source_kind, o.c.source_id, o.c.external_id, o.c.title)
        .join(o, o.c.id == n.c.object_id)
        .where(
            n.c.project_id == project_id,
            n.c.key.in_(keys),
            n.c.object_id.in_(_visible_ids(conn, project_id)),
        )
        .order_by(n.c.key, n.c.role.desc(), n.c.id)  # role 'item' before 'derived'
    ).mappings()
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        node = by_key.setdefault(
            row["key"],
            {
                "key": row["key"],
                "type": row["type"],
                "label": row["label"],
                "attributes": dict(row["attributes"] or {}),
                "items": [],
                "asserted_by": 0,
            },
        )
        node["asserted_by"] += 1
        if row["role"] == "item":
            node["items"].append(
                Item(
                    kind=row["source_kind"],
                    source_id=row["source_id"],
                    external_id=row["external_id"],
                    title=row["title"] or "",
                )
            )
    return [by_key[k] for k in keys if k in by_key]


def _with_degree(conn: Connection, project_id: uuid.UUID, nodes: list[dict[str, Any]]) -> list[Node]:
    keys = [n["key"] for n in nodes]
    degree = dict.fromkeys(keys, 0)
    for m in edges(conn, project_id, keys=keys) if keys else []:
        for k in {m["from_key"], m["to_key"]}:
            if k in degree:
                degree[k] += 1
    return [Node(**n, degree=degree[n["key"]]) for n in nodes]


def _edge(m: dict[str, Any]) -> Edge:
    return Edge(**{k: m[k] for k in Edge.model_fields})


# ---------------------------------------------------------------- routes


@router.get(
    "/v1/projects/{project_id}/graph/nodes",
    response_model=NodePage,
    summary="Graph nodes of visible content, by key; filter by type or text",
    responses={404: PROBLEM},
)
def list_nodes(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    type: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=1000)] = None,
) -> NodePage:
    n = graph_nodes
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        stmt = (
            select(n.c.key)
            .distinct()
            .where(n.c.project_id == project_id, n.c.object_id.in_(_visible_ids(conn, project_id)))
        )
        if type:
            stmt = stmt.where(n.c.type == type)
        if q:
            like = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
            stmt = stmt.where(or_(n.c.key.ilike(like), n.c.label.ilike(like)))
        if cursor:
            stmt = stmt.where(n.c.key > cursor)
        keys = list(conn.execute(stmt.order_by(n.c.key).limit(limit + 1)).scalars())
        page, more = keys[:limit], len(keys) > limit
        items = _with_degree(conn, project_id, _nodes(conn, project_id, page))
    return NodePage(items=items, next=page[-1] if more else None)


@router.get(
    "/v1/projects/{project_id}/graph/nodes/{key:path}",
    response_model=NodeDetail,
    summary="A graph node with its edges in both directions, each with origin, status and evidence",
    responses={404: PROBLEM},
)
def get_node(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    key: str,
    include_rejected: bool = False,
) -> NodeDetail:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        found = _nodes(conn, project_id, [key])
        if not found:
            raise ApiError(
                404,
                "GRAPH_NODE_NOT_FOUND",
                "Graph node not found",
                "No visible content of this project carries this key",
            )
        all_edges = edges(conn, project_id, keys=[key], include_rejected=include_rejected)
        [node] = _with_degree(conn, project_id, found)
    return NodeDetail(
        node=node,
        outgoing=[_edge(m) for m in all_edges if m["from_key"] == key],
        incoming=[_edge(m) for m in all_edges if m["to_key"] == key and m["from_key"] != key],
    )


@router.get(
    "/v1/projects/{project_id}/graph/edges",
    response_model=EdgePage,
    summary="Graph edges of visible content, merged by natural key; filter by kind, origin and status",
    responses={404: PROBLEM},
)
def list_edges(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    kind: Annotated[str | None, Query(max_length=64)] = None,
    origin: Literal["validated", "inferred", "manual"] | None = None,
    status: Literal["active", "confirmed", "rejected"] | None = None,
    unresolved: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[int | None, Query(ge=0)] = None,
) -> EdgePage:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        found = edges(conn, project_id, kind=kind, include_rejected=status == "rejected")
    found = [
        m
        for m in found
        if (origin is None or m["origin"] == origin)
        and (status is None or m["status"] == status)
        and (unresolved is None or m["unresolved"] == unresolved)
    ]
    start = cursor or 0
    page = found[start : start + limit]
    return EdgePage(
        items=[_edge(m) for m in page], next=start + limit if start + limit < len(found) else None
    )


@router.get(
    "/v1/projects/{project_id}/graph/status",
    response_model=GraphStatus,
    summary="Graph health: counts, orphan nodes, unresolved references and items pending extraction",
    responses={404: PROBLEM},
)
def graph_status(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> GraphStatus:
    from app.content import graph

    n = graph_nodes
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        visible_nodes = conn.execute(
            select(n.c.key, n.c.type, n.c.role).where(
                n.c.project_id == project_id, n.c.object_id.in_(_visible_ids(conn, project_id))
            )
        ).all()
        found = edges(conn, project_id, include_rejected=True)
        pending = graph.pending(conn, project_id)
    types: dict[str, str] = {}
    items: set[str] = set()
    for key, type_, role in visible_nodes:
        if role == "item" or key not in types:
            types[key] = type_
        if role == "item":
            items.add(key)
    live = [m for m in found if m["status"] != "rejected"]
    touched = {k for m in live for k in (m["from_key"], m["to_key"])}
    orphans = sorted(items - touched)
    missing: dict[str, int] = {}
    for m in live:
        for k in (m["from_key"], m["to_key"]):
            if m["unresolved"] and k not in types:
                missing[k] = missing.get(k, 0) + 1

    def counts(values) -> list[Count]:
        tally: dict[str, int] = {}
        for v in values:
            tally[v] = tally.get(v, 0) + 1
        return [Count(name=k, count=v) for k, v in sorted(tally.items())]

    return GraphStatus(
        extractor=graph.EXTRACTOR,
        pending=pending,
        nodes=len(types),
        edges=len(live),
        node_types=counts(types.values()),
        edge_kinds=counts(m["kind"] for m in live),
        origins=counts(m["origin"] for m in live),
        statuses=counts(m["status"] for m in found),
        orphans=len(orphans),
        orphan_keys=orphans[:LISTED],
        unresolved=len(missing),
        unresolved_keys=[
            Unresolved(key=k, references=v) for k, v in sorted(missing.items(), key=lambda x: (-x[1], x[0]))
        ][:LISTED],
    )


# ---------------------------------------------------------------- review (INPROOM-218)

KIND_PATTERN = r"^[a-z][a-z0-9_]{1,40}$"


class ReviewRequest(BaseModel):
    action: Literal["confirm", "reject", "correct"]
    to: str | None = Field(default=None, min_length=1, max_length=1000)  # correct: the right target key
    reason: str = Field(min_length=1, max_length=500)


class ManualEdgeRequest(BaseModel):
    from_key: str = Field(min_length=1, max_length=1000)
    kind: str = Field(pattern=KIND_PATTERN)
    to_key: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=500)


class ReviewResult(BaseModel):
    edge: Edge  # the reviewed edge, with its new status
    corrected: Edge | None  # correct: the manual edge to the right target


def _not_found(what: str) -> ApiError:
    return ApiError(404, "GRAPH_EDGE_NOT_FOUND", "Graph edge not found", what)


def _find(conn: Connection, project_id: uuid.UUID, edge_id: int) -> dict[str, Any]:
    """The visible (merged) edge an id names: a stating row's id, or minus a manual edge's review id."""
    if edge_id > 0:
        row = conn.execute(
            select(graph_edges.c.from_key, graph_edges.c.kind, graph_edges.c.to_key).where(
                graph_edges.c.project_id == project_id, graph_edges.c.id == edge_id
            )
        ).first()
    else:
        row = conn.execute(
            select(graph_reviews.c.from_key, graph_reviews.c.kind, graph_reviews.c.to_key).where(
                graph_reviews.c.project_id == project_id,
                graph_reviews.c.id == -edge_id,
                graph_reviews.c.action == "manual",
            )
        ).first()
    if row is None:
        raise _not_found("No edge of this project has this id")
    for m in edges(conn, project_id, keys=[row.from_key], kind=row.kind, include_rejected=True):
        if (m["from_key"], m["kind"], m["to_key"]) == tuple(row):
            return m
    raise _not_found("The edge is not visible: its content is withheld or gone")


def _upsert_review(conn, principal, project_id, natural, action: str, reason: str) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    values = {"action": action, "reason": reason, "actor": principal.user_id, "at": func.now()}
    conn.execute(
        pg_insert(graph_reviews)
        .values(
            tenant_id=principal.tenant_id,
            project_id=project_id,
            from_key=natural[0],
            kind=natural[1],
            to_key=natural[2],
            **values,
        )
        .on_conflict_do_update(constraint="uq_graph_reviews_edge", set_=values)
    )


def _audit(conn, principal, request, project_id, action: str, natural, reason: str, **extra) -> None:
    from app import audit

    audit.record(
        conn,
        principal,
        action,
        "graph_edge",
        f"{natural[0]} {natural[1]} {natural[2]}"[:500],
        trace_id=getattr(request.state, "trace_id", None),
        payload={"from_key": natural[0], "kind": natural[1], "to_key": natural[2], "reason": reason} | extra,
        project_id=project_id,
    )


def _writable(conn, principal, request, project_id) -> None:
    from app.api_errors import conflict

    access = authorize(conn, principal, request, "project.graph.curate", project_id)
    if access.project["status"] == "ARCHIVED":
        raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")


def _require_nodes(conn, project_id, *keys: str) -> None:
    found = {n["key"] for n in _nodes(conn, project_id, list(keys))}
    missing = [k for k in keys if k not in found]
    if missing:
        raise ApiError(
            404,
            "GRAPH_NODE_NOT_FOUND",
            "Graph node not found",
            f"No visible content of this project carries {missing[0]}",
        )


def _one(conn, project_id, natural) -> Edge:
    for m in edges(conn, project_id, keys=[natural[0]], kind=natural[1], include_rejected=True):
        if (m["from_key"], m["kind"], m["to_key"]) == natural:
            return _edge(m)
    raise _not_found("The edge is not visible")


@router.post(
    "/v1/projects/{project_id}/graph/edges/{edge_id}/review",
    response_model=ReviewResult,
    summary="Confirm, reject or correct a graph edge; durable across rebuilds and audited",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
def review_edge(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    edge_id: int,
    payload: Annotated[ReviewRequest, Body()],
) -> ReviewResult:
    """Reviews are keyed by the edge's natural key (from, kind, to), so re-extraction never undoes them: a
    rejected edge stays hidden however often the graph is rebuilt. `correct` rejects the edge and adds a
    manual edge of the same kind to `to` (a visible node)."""
    if payload.action == "correct" and not payload.to:
        raise ApiError(422, "GRAPH_CORRECTION_TARGET_REQUIRED", "Correction needs a target", "Send `to`")
    with tenant_session(principal.tenant_id) as conn:
        _writable(conn, principal, request, project_id)
        edge = _find(conn, project_id, edge_id)
        natural = (edge["from_key"], edge["kind"], edge["to_key"])
        corrected = None
        if payload.action == "confirm":
            if edge["origin"] == "manual":
                raise ApiError(
                    409, "GRAPH_EDGE_MANUAL", "A manual edge is confirmed already", "Nothing to do"
                )
            _upsert_review(conn, principal, project_id, natural, "confirm", payload.reason)
        else:
            if payload.action == "correct":
                target = (natural[0], natural[1], payload.to)
                if payload.to == natural[2]:
                    raise ApiError(
                        409, "GRAPH_CORRECTION_SAME_TARGET", "Same target", "Choose a different node"
                    )
                _require_nodes(conn, project_id, payload.to)
                _upsert_review(conn, principal, project_id, target, "manual", payload.reason)
            _upsert_review(conn, principal, project_id, natural, "reject", payload.reason)
            if payload.action == "correct":
                corrected = _one(conn, project_id, target)
        _audit(
            conn,
            principal,
            request,
            project_id,
            "graph.edge.reviewed",
            natural,
            payload.reason,
            review=payload.action,
            origin=edge["origin"],
            corrected_to=payload.to if payload.action == "correct" else None,
        )
        if edge["origin"] == "manual" and payload.action != "confirm":
            # a rejected manual edge exists only as its reject review now
            review = {"action": "reject", "reason": payload.reason, "actor": principal.user_id, "at": None}
            reviewed = Edge(
                **{k: edge[k] for k in Edge.model_fields} | {"status": "rejected", "reviewed": review}
            )
        else:
            reviewed = _one(conn, project_id, natural)
    return ReviewResult(edge=reviewed, corrected=corrected)


@router.post(
    "/v1/projects/{project_id}/graph/edges",
    status_code=201,
    response_model=Edge,
    summary="Add (or restate) a manual graph edge between two visible nodes; audited",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
def add_edge(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[ManualEdgeRequest, Body()],
) -> Edge:
    """Idempotent: restating a manual edge updates its reason. An edge the extractor already states is a
    409 (confirm it instead); a rejected one is replaced by the manual edge."""
    natural = (payload.from_key, payload.kind, payload.to_key)
    if payload.from_key == payload.to_key:
        raise ApiError(422, "GRAPH_EDGE_SELF", "An edge needs two different nodes", "Choose another target")
    with tenant_session(principal.tenant_id) as conn:
        _writable(conn, principal, request, project_id)
        _require_nodes(conn, project_id, payload.from_key, payload.to_key)
        for m in edges(conn, project_id, keys=[payload.from_key], kind=payload.kind, include_rejected=True):
            if (m["from_key"], m["kind"], m["to_key"]) == natural and m["origin"] != "manual":
                if m["status"] != "rejected":
                    raise ApiError(
                        409,
                        "GRAPH_EDGE_EXISTS",
                        "The graph already has this edge",
                        f"Confirm edge {m['id']} instead",
                    )
        _upsert_review(conn, principal, project_id, natural, "manual", payload.reason)
        _audit(conn, principal, request, project_id, "graph.edge.created", natural, payload.reason)
        edge = _one(conn, project_id, natural)
    return edge
