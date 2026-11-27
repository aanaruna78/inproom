"""The knowledge graph (INPROOM-34): nodes and edges extracted from the content store, with evidence.

A projection, like chunks and vectors: rebuilt per item by the `graph` job, deleted with the content, and
filtered by the visibility rule when read. Only curators' reviews (`graph_reviews`) are durable user data.

**Nodes** have natural keys (`node_key`): `jira:PAY-17`, `confluence:101`, `confluence:att:7`, `doc:<id>`,
`file:<repo>:<branch>:<path>`, `component:<PROJ>:<name>`, `version:<PROJ>:<name>`, `owner:<@team>`,
`commit:<sha>`. `graph_nodes` holds *occurrences*: one row per (key, item that asserts it). An item's own
node has role `item`; the entities it names have role `derived`. The same key from several items is one node.

**Edges** go from key to key, so a target that arrives later resolves without re-extraction; a key that no
node carries is an unresolved reference. Each edge row belongs to the item whose extraction produced it,
with its origin (`validated`: stated by a source-system field; `inferred`: read from text), confidence,
extractor version and evidence (a citation of the item's revision, plus the field or matched text).

**The job** re-extracts only items whose current revision (or the extractor version) changed, replacing that
item's rows in one transaction: retries and re-runs never duplicate. Items whose current revision was purged,
or that left the store, lose their rows. Withheld items keep them: visibility hides them when read, and a
restored item's graph returns as it was.
"""

import json
import logging
import uuid
from typing import Any

from sqlalchemy import Connection, delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.content import store as blobs
from app.db import tenant_session
from app.db_schema import (
    code_snapshots,
    confluence_pages,
    graph_edges,
    graph_nodes,
    graph_sources,
    project_jira_sources,
    project_repositories,
    projects,
    source_objects,
    source_revisions,
)
from app.sync import queue
from app.sync.worker import JobContext, SkipJob, handler

log = logging.getLogger(__name__)
EXTRACTOR = "graph@1"
JOB_LIMIT = 200  # items per run; the job requeues itself while more remain
VALIDATED = 1.0  # confidence of an edge a source field states

Node = dict[str, Any]
Edge = dict[str, Any]


# ---------------------------------------------------------------- keys and pure extraction


def file_key(repo: str, branch: str, path: str) -> str:
    return f"file:{repo}:{branch}:{path}"


def node_key(kind: str, external_id: str, *, jira_key: str | None = None, repo: str | None = None) -> str:
    """The natural key of an item's own node. Pure (unit-tested)."""
    if kind == "jira":
        return f"jira:{jira_key or external_id}"
    if kind == "confluence":
        return f"confluence:{external_id}"
    if kind == "upload":
        return f"doc:{external_id}"
    if kind == "git":
        branch, _, path = external_id.partition(":")
        return file_key(repo or "", branch, path)
    return f"{kind}:{external_id}"


def _node(key: str, type_: str, label: str, role: str = "derived", **attributes: Any) -> Node:
    return {"key": key, "type": type_, "label": label, "role": role, "attributes": attributes}


def _edge(from_key: str, kind: str, to_key: str, field: str, value: Any = None) -> Edge:
    evidence: dict[str, Any] = {"field": field}
    if value is not None:
        evidence["value"] = value
    return {
        "from_key": from_key,
        "kind": kind,
        "to_key": to_key,
        "origin": "validated",
        "confidence": VALIDATED,
        "evidence": evidence,
    }


def _issue_type(fields: dict[str, Any]) -> str:
    from app.modules.jira_schema import canonical_issue_type

    t = fields.get("issuetype") or {}
    if not isinstance(t, dict) or not t.get("name"):
        return "other"
    return canonical_issue_type(t["name"], int(t.get("hierarchyLevel") or 0), bool(t.get("subtask")))


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("value")
    return value if isinstance(value, str) else None


def jira_graph(record: dict[str, Any], epic_field: str | None = None) -> tuple[list[Node], list[Edge]]:
    """Nodes and validated edges stated by one stored Jira issue record (app.content.ingest.jira_record).
    Pure (unit-tested). Issue links are directed as Jira states them: outward `this -> other`, inward
    `other -> this`, so both ends' records produce the same natural edge."""
    fields = record.get("fields") or {}
    key = record.get("key") or str(record.get("id"))
    me = f"jira:{key}"
    project = key.split("-", 1)[0] if "-" in key else key
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    nodes = [
        _node(
            me,
            "jira_issue",
            f"{key}: {fields.get('summary') or ''}".strip(": "),
            role="item",
            issue_type=_issue_type(fields),
            status=(status or {}).get("name"),
            labels=list(fields.get("labels") or []),
        )
    ]
    edges: list[Edge] = []
    parent = fields.get("parent")
    if isinstance(parent, dict) and parent.get("key"):
        edges.append(_edge(me, "child_of", f"jira:{parent['key']}", "parent", parent["key"]))
    for sub in fields.get("subtasks") or []:
        if isinstance(sub, dict) and sub.get("key"):
            edges.append(_edge(f"jira:{sub['key']}", "child_of", me, "subtasks", sub["key"]))
    epic = fields.get(epic_field) if epic_field else None
    if isinstance(epic, str) and epic:
        edges.append(_edge(me, "in_epic", f"jira:{epic}", epic_field, epic))
    for link in fields.get("issuelinks") or []:
        kind = "_".join(str(link.get("type") or "relates").lower().split())
        if isinstance(link.get("outwardIssue"), dict) and link["outwardIssue"].get("key"):
            other = link["outwardIssue"]["key"]
            edges.append(_edge(me, kind, f"jira:{other}", "issuelinks", other))
        if isinstance(link.get("inwardIssue"), dict) and link["inwardIssue"].get("key"):
            other = link["inwardIssue"]["key"]
            edges.append(_edge(f"jira:{other}", kind, me, "issuelinks", other))
    for field, kind, prefix, type_ in (
        ("components", "in_component", "component", "jira_component"),
        ("fixVersions", "fix_version", "version", "jira_version"),
        ("versions", "affects_version", "version", "jira_version"),
    ):
        for value in fields.get(field) or []:
            name = _name(value)
            if name:
                target = f"{prefix}:{project}:{name}"
                nodes.append(_node(target, type_, name, project=project))
                edges.append(_edge(me, kind, target, field, name))
    return _unique_nodes(nodes), _unique_edges(edges)


def _unique_nodes(nodes: list[Node]) -> list[Node]:
    out: dict[str, Node] = {}
    for n in nodes:
        out.setdefault(n["key"], n)
    return list(out.values())


def _unique_edges(edges: list[Edge]) -> list[Edge]:
    out: dict[tuple[str, str, str], Edge] = {}
    for e in edges:
        out.setdefault((e["from_key"], e["kind"], e["to_key"]), e)
    return list(out.values())


# ---------------------------------------------------------------- per-item extraction (reads the database)


class _Context:
    """Per-run lookups shared by the items of one project."""

    def __init__(self, conn: Connection, project_id: uuid.UUID):
        self.conn = conn
        self.repos = {
            r.id: r.full_name
            for r in conn.execute(
                select(project_repositories.c.id, project_repositories.c.full_name).where(
                    project_repositories.c.project_id == project_id
                )
            )
        }
        self.epic_fields: dict[uuid.UUID, str | None] = {}
        for r in conn.execute(
            select(project_jira_sources.c.id, project_jira_sources.c.mapping).where(
                project_jira_sources.c.project_id == project_id
            )
        ):
            entry = ((r.mapping or {}).get("fields") or {}).get("epic_link") or {}
            self.epic_fields[r.id] = entry.get("field_id") if isinstance(entry, dict) else None
        self._owners: dict[tuple, list] = {}

    def owners(self, source_id: uuid.UUID, branch: str, commit: str | None, path: str) -> list[str]:
        from app.content.code.codeowners import owners_of, parse

        key = (source_id, branch, commit)
        if key not in self._owners:
            s = code_snapshots.c
            stmt = select(s.codeowners).where(s.source_id == source_id, s.branch == branch)
            stmt = stmt.where(s.commit_sha == commit) if commit else stmt
            text_ = ((self.conn.execute(stmt.order_by(s.started_at.desc()).limit(1)).scalar()) or {}).get(
                "text"
            )
            self._owners[key] = parse(text_)[0] if text_ else []
        return owners_of(self._owners[key], path)

    def page_parent(self, source_id: uuid.UUID, page_id: str) -> str | None:
        return self.conn.execute(
            select(confluence_pages.c.parent_page_id).where(
                confluence_pages.c.source_id == source_id, confluence_pages.c.page_id == page_id
            )
        ).scalar()


def _citation(obj: dict[str, Any], rev: dict[str, Any], anchor: str | None = None) -> dict[str, Any]:
    return {
        "kind": obj["source_kind"],
        "source_id": str(obj["source_id"]),
        "external_id": obj["external_id"],
        "seq": rev["seq"],
        "anchor": anchor,
    }


def extract_item(
    ctx: _Context, store: blobs.BlobStore, obj: dict[str, Any], rev: dict[str, Any]
) -> tuple[list[Node], list[Edge]]:
    """The nodes and validated edges one item's current revision states. Store outages propagate."""
    kind, external = obj["source_kind"], obj["external_id"]
    if kind == "jira":
        record = json.loads(blobs.read(store, rev["blob_key"]))
        nodes, edges = jira_graph(record, ctx.epic_fields.get(obj["source_id"]))
    elif kind == "confluence":
        me = node_key(kind, external)
        if external.startswith("att:"):
            nodes = [_node(me, "attachment", obj["title"], role="item")]
            parent = obj.get("parent_external_id")
            edges = (
                [_edge(me, "attached_to", f"confluence:{parent}", "attachments", parent)] if parent else []
            )
        else:
            nodes = [_node(me, "confluence_page", obj["title"], role="item")]
            parent = ctx.page_parent(obj["source_id"], external)
            edges = (
                [_edge(me, "child_of", f"confluence:{parent}", "parent_page_id", parent)] if parent else []
            )
    elif kind == "git":
        repo = ctx.repos.get(obj["source_id"], "")
        branch, _, path = external.partition(":")
        me = node_key(kind, external, repo=repo)
        nodes = [_node(me, "code_file", path, role="item", repository=repo, branch=branch)]
        edges = []
        for owner in ctx.owners(obj["source_id"], branch, obj.get("verified_ref"), path):
            nodes.append(_node(f"owner:{owner}", "owner", owner))
            edges.append(_edge(me, "owned_by", f"owner:{owner}", "CODEOWNERS", owner))
        if obj.get("verified_ref"):
            sha = obj["verified_ref"]
            nodes.append(_node(f"commit:{sha}", "commit", sha[:12], repository=repo, branch=branch))
            edges.append(_edge(me, "at_commit", f"commit:{sha}", "verified_ref", sha))
    elif kind == "upload":
        nodes, edges = [_node(node_key(kind, external), "document", obj["title"], role="item")], []
    else:
        nodes, edges = [], []
    citation = _citation(obj, rev)
    for e in edges:
        e["evidence"] = [e["evidence"] | {"citation": citation}]
    return nodes, edges


# ---------------------------------------------------------------- the job


def queue_graph(conn: Connection, tenant: uuid.UUID, project_id: uuid.UUID, reason: str = "schedule") -> None:
    queue.enqueue(conn, tenant, project_id, "graph", project_id, reason)


def _drop(conn: Connection, object_ids: list) -> None:
    if object_ids:
        for table in (graph_edges, graph_nodes, graph_sources):
            conn.execute(delete(table).where(table.c.object_id.in_(object_ids)))


def drop_revisions(conn: Connection, revision_ids: list) -> None:
    """Retention: purged revisions leave the graph in the purge's transaction."""
    if revision_ids:
        for table in (graph_edges, graph_nodes, graph_sources):
            conn.execute(delete(table).where(table.c.revision_id.in_(revision_ids)))


def _stale(conn: Connection, project_id: uuid.UUID, limit: int) -> tuple[list[dict], list, bool]:
    """(items to (re)extract with their current revision, object ids to drop, more remain)."""
    o, r, g = source_objects, source_revisions, graph_sources
    current = (
        select(o, r.c.seq, r.c.blob_key, r.c.id.label("rev_id"))
        .join(r, r.c.id == o.c.current_revision_id)
        .outerjoin(g, g.c.object_id == o.c.id)
        .where(
            o.c.project_id == project_id,
            r.c.purged_at.is_(None),
            (g.c.id.is_(None)) | (g.c.revision_id != o.c.current_revision_id) | (g.c.extractor != EXTRACTOR),
        )
        .order_by(o.c.id)
        .limit(limit + 1)
    )
    rows = [dict(x) for x in conn.execute(current).mappings()]
    gone = list(
        conn.execute(
            select(g.c.object_id)
            .outerjoin(o, o.c.id == g.c.object_id)
            .outerjoin(r, r.c.id == o.c.current_revision_id)
            .where(g.c.project_id == project_id, (o.c.id.is_(None)) | (r.c.purged_at.is_not(None)))
        ).scalars()
    )
    return rows[:limit], gone, len(rows) > limit


def _write(conn, tenant, project_id, obj: dict, nodes: list[Node], edges: list[Edge]) -> None:
    _drop(conn, [obj["id"]])
    base = {
        "tenant_id": tenant,
        "project_id": project_id,
        "object_id": obj["id"],
        "revision_id": obj["rev_id"],
    }
    if nodes:
        conn.execute(insert(graph_nodes), [base | n for n in nodes])
    if edges:
        conn.execute(insert(graph_edges), [base | e | {"extractor": EXTRACTOR} for e in edges])
    conn.execute(
        pg_insert(graph_sources)
        .values(**base, extractor=EXTRACTOR)
        .on_conflict_do_update(
            index_elements=["object_id"], set_={"revision_id": obj["rev_id"], "extractor": EXTRACTOR}
        )
    )


def run(tenant: uuid.UUID, project_id: uuid.UUID, limit: int = JOB_LIMIT) -> dict[str, Any]:
    store = blobs.get_store()
    counts = {"items": 0, "nodes": 0, "edges": 0, "dropped": 0}
    with tenant_session(tenant) as conn:
        items, gone, more = _stale(conn, project_id, limit)
        _drop(conn, gone)
        counts["dropped"] = len(gone)
    for obj in items:
        with tenant_session(tenant) as conn:  # one item per transaction: its rows change atomically
            ctx = _Context(conn, project_id)
            rev = {"seq": obj["seq"], "blob_key": obj["blob_key"]}
            try:
                nodes, edges = extract_item(ctx, store, obj, rev)
            except (blobs.BlobMissing, blobs.BlobCorrupt, ValueError) as exc:
                log.warning("graph: item %s not extracted: %s", obj["id"], exc)
                nodes, edges = [], []
            _write(conn, tenant, project_id, obj, nodes, edges)
        counts["items"] += 1
        counts["nodes"] += len(nodes)
        counts["edges"] += len(edges)
    counts["more"] = more
    return counts


@handler("graph")
def graph(ctx: JobContext) -> dict[str, Any]:
    tenant, project_id = ctx.tenant, ctx.job["project_id"]
    with tenant_session(tenant) as conn:
        status = conn.execute(select(projects.c.status).where(projects.c.id == project_id)).scalar()
    if status in (None, "ARCHIVED"):
        raise SkipJob("project is archived or gone")
    result = run(tenant, project_id)  # store outages propagate: the worker retries with backoff
    with tenant_session(tenant) as conn:
        if result["more"]:
            queue_graph(conn, tenant, project_id, ctx.job["reason"])
        queue.succeed(conn, ctx.job, result)
    log.info("knowledge graph project=%s %s", project_id, json.dumps(result))
    return result


__all__ = ["EXTRACTOR", "drop_revisions", "extract_item", "jira_graph", "node_key", "queue_graph", "run"]
