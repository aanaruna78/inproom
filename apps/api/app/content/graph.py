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
import re
import uuid
from typing import Any

from sqlalchemy import Connection, delete, exists, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.content import store as blobs
from app.db import tenant_session
from app.db_schema import (
    code_snapshots,
    confluence_pages,
    derivatives,
    graph_edges,
    graph_nodes,
    graph_sources,
    knowledge_chunks,
    project_jira_sources,
    project_repositories,
    projects,
    source_objects,
    source_revisions,
)
from app.sync import queue
from app.sync.worker import JobContext, SkipJob, handler

log = logging.getLogger(__name__)
EXTRACTOR = "graph@1"  # bump when extraction changes: every item is re-extracted
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


# ---------------------------------------------------------------- mentions in text (inferred edges)

_JIRA_KEY = re.compile(r"(?<![A-Za-z0-9_/-])([A-Z][A-Z0-9_]{1,9})-([1-9][0-9]{0,6})(?![A-Za-z0-9_-])")
# key-shaped tokens that are standards, encodings or algorithms, not issues (unknown prefixes only)
_NOT_ISSUES = frozenset(
    "UTF ISO SHA MD RFC CVE HTTP TLS SSL IPV AES RSA GPT ECMA PEP ES COVID WCAG OWASP EAN UPC DDR USB PCI "
    "SOC GDPR FIPS NIST X509 BS EN IEC IEEE ANSI ASCII".split()
)
_PAGE_LINK = re.compile(r"(?:/pages/(?:viewpage\.action\?pageId=)?|[?&]pageId=)([0-9]{2,15})")
_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w/])")
CODE_EXTENSIONS = frozenset(
    "py ts tsx js jsx go java kt rb rs cs cpp cc c h hpp php swift scala sql sh yml yaml json toml md".split()
)
EXCERPT = 60  # characters of context either side of a match, in evidence
MAX_EVIDENCE = 5  # chunks cited per inferred edge


def mentions(
    text: str, *, jira_projects: set[str], files: dict[str, list[str]]
) -> list[tuple[str, float, str]]:
    """(target key, confidence, matched text) for each thing `text` mentions. Pure (unit-tested).

    - Jira keys: 0.9 when the prefix is a Jira project of this project's sources, else 0.5 (an unresolved
      reference unless some node carries it); well-known non-issue prefixes (UTF-8, SHA-256, ISO-8601) are
      ignored for unknown prefixes.
    - Confluence page links (`/pages/123`, `pageId=123`): 0.9.
    - File paths with a directory (`src/billing.py`): 0.8 to every code file at that path (`files`: path ->
      file keys); an unknown path with a code extension is an unresolved `path:` reference at 0.4.
    """
    out: dict[str, tuple[float, str]] = {}

    def add(key: str, confidence: float, matched: str) -> None:
        if key not in out or out[key][0] < confidence:
            out[key] = (confidence, matched)

    for m in _JIRA_KEY.finditer(text):
        prefix = m.group(1)
        if prefix in jira_projects:
            add(f"jira:{m.group(0)}", 0.9, m.group(0))
        elif prefix not in _NOT_ISSUES:
            add(f"jira:{m.group(0)}", 0.5, m.group(0))
    for m in _PAGE_LINK.finditer(text):
        add(f"confluence:{m.group(1)}", 0.9, m.group(0))
    for m in _PATH.finditer(text):
        path = m.group(1).lstrip("./")
        if "://" in text[max(0, m.start() - 12) : m.start() + 3] or path.startswith(("http", "www.")):
            continue  # part of a URL
        if path in files:
            for key in files[path]:
                add(key, 0.8, path)
        elif path.rsplit(".", 1)[-1].lower() in CODE_EXTENSIONS:
            add(f"path:{path}", 0.4, path)
    return [(k, c, t) for k, (c, t) in out.items()]


def _excerpt(text: str, matched: str) -> str:
    at = text.find(matched)
    if at < 0:
        return matched
    start, end = max(0, at - EXCERPT), min(len(text), at + len(matched) + EXCERPT)
    return ("…" if start else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")


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
        self.jira_projects = set(
            conn.execute(
                select(project_jira_sources.c.jira_key).where(project_jira_sources.c.project_id == project_id)
            ).scalars()
        )
        self.files: dict[str, list[str]] = {}
        for r in conn.execute(
            select(source_objects.c.source_id, source_objects.c.external_id).where(
                source_objects.c.project_id == project_id,
                source_objects.c.source_kind == "git",
                source_objects.c.current_revision_id.is_not(None),
            )
        ):
            branch, _, path = r.external_id.partition(":")
            self.files.setdefault(path, []).append(file_key(self.repos.get(r.source_id, ""), branch, path))

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
    me = nodes[0]["key"] if nodes else None
    if me is not None:
        edges += _inferred(ctx, obj, rev, me)
    return nodes, edges


def _inferred(ctx: _Context, obj: dict[str, Any], rev: dict[str, Any], me: str) -> list[Edge]:
    """Inferred edges of one item: what its chunks mention, and items with identical content."""
    k = knowledge_chunks
    found: dict[str, Edge] = {}
    chunks = ctx.conn.execute(
        select(k.c.anchor, k.c.context, k.c.text)
        .where(k.c.object_id == obj["id"], k.c.revision_id == obj["rev_id"])
        .order_by(k.c.ordinal)
    )
    for chunk in chunks:
        body = f"{chunk.context}\n{chunk.text}" if chunk.context else chunk.text
        for target, confidence, matched in mentions(body, jira_projects=ctx.jira_projects, files=ctx.files):
            if target == me:
                continue  # an item naming itself
            edge = found.setdefault(
                target,
                {
                    "from_key": me,
                    "kind": "mentions",
                    "to_key": target,
                    "origin": "inferred",
                    "confidence": confidence,
                    "evidence": [],
                },
            )
            edge["confidence"] = max(edge["confidence"], confidence)
            if len(edge["evidence"]) < MAX_EVIDENCE:
                edge["evidence"].append(
                    {
                        "citation": _citation(obj, rev, chunk.anchor),
                        "text": matched,
                        "excerpt": _excerpt(body, matched),
                    }
                )
    edges = list(found.values())
    o, r = source_objects, source_revisions
    twins = ctx.conn.execute(  # identical content held by another item of the project
        select(o.c.source_kind, o.c.external_id, o.c.source_id, o.c.title)
        .join(r, r.c.id == o.c.current_revision_id)
        .where(
            o.c.project_id == obj["project_id"],
            o.c.id != obj["id"],
            r.c.content_sha256 == obj["content_sha256"],
            r.c.purged_at.is_(None),
        )
    )
    for twin in twins:
        other = node_key(
            twin.source_kind,
            twin.external_id,
            jira_key=(twin.title or "").split(":", 1)[0] or None,
            repo=ctx.repos.get(twin.source_id),
        )
        if other == me:
            continue
        a, b = sorted((me, other))  # one natural edge, whichever twin states it
        edges.append(
            {
                "from_key": a,
                "kind": "duplicates",
                "to_key": b,
                "origin": "inferred",
                "confidence": 0.95,
                "evidence": [
                    {
                        "citation": _citation(obj, rev),
                        "field": "content_sha256",
                        "value": obj["content_sha256"],
                    }
                ],
            }
        )
    return edges


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
    o, r, g, d = source_objects, source_revisions, graph_sources, derivatives
    current = (
        select(o, r.c.seq, r.c.blob_key, r.c.content_sha256, r.c.id.label("rev_id"))
        .join(r, r.c.id == o.c.current_revision_id)
        .outerjoin(g, g.c.object_id == o.c.id)
        .where(
            o.c.project_id == project_id,
            r.c.purged_at.is_(None),
            (g.c.id.is_(None))
            | (g.c.revision_id != o.c.current_revision_id)
            | (g.c.extractor != EXTRACTOR)
            | exists().where(  # its chunks (mention evidence) were built after the last extraction
                d.c.revision_id == o.c.current_revision_id,
                d.c.producer == "chunks",
                d.c.status == "READY",
                d.c.built_at > g.c.extracted_at,
            ),
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
            index_elements=["object_id"],
            set_={"revision_id": obj["rev_id"], "extractor": EXTRACTOR, "extracted_at": func.now()},
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


__all__ = [
    "EXTRACTOR",
    "drop_revisions",
    "extract_item",
    "jira_graph",
    "mentions",
    "node_key",
    "queue_graph",
    "run",
]
