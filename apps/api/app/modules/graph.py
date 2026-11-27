"""Knowledge graph read API (INPROOM-217 / story INPROOM-34). See app/content/graph.py for the model.

Everything returned passes the visibility rule (app.content.visibility): a node is shown when at least one
of its occurrences belongs to a visible item, an edge when the item that stated it is visible, and evidence
only from visible items. An edge whose endpoint exists but is hidden is hidden too, so a restricted item is
never revealed through a link; an endpoint no item carries at all is an **unresolved reference** (shown,
flagged). Rows from several items stating the same edge are merged by natural key (from, kind, to): origin
is `validated` when any source field states it, confidence is the highest, evidence is combined. Curators'
reviews (INPROOM-218) set `status`: `confirmed`, `rejected` (hidden unless asked for) or `active`.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import Connection, func, or_, select

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
    stmt = select(e).where(e.c.project_id == project_id, e.c.object_id.in_(_visible_ids(conn, project_id)))
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
