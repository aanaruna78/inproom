"""Code search, snapshot status and file symbols (INPROOM-175 / story INPROOM-29).

Every hit is a citation: repository, branch, commit, path, symbol and line range, with the blob sha (the
revision identity), owners from CODEOWNERS at that commit, and a commit-pinned permalink.

Which commit a hit cites: when the branch's latest snapshot is COMPLETE, every indexed file is verified at
that commit, so the hit cites it; when the snapshot is PARTIAL, the hit cites the commit at which its own
content was recorded (always true). `behind` says the branch head has moved past the indexed commit.
Only READY symbols of the current revision of a CURRENT file in an ACTIVE repository are searchable.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import Connection, case, func, select, tuple_

from app.api_errors import ApiError
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.content import store as blobs
from app.content.code.codeowners import owners_of, parse
from app.db import tenant_session
from app.db_schema import (
    code_snapshots,
    code_symbols,
    derivatives,
    project_repositories,
    source_connections,
    source_objects,
    source_sync_state,
)

router = APIRouter(tags=["code"])
PROBLEM = {"description": "problem+json"}
KINDS = "function|method|constructor|class|interface|type|enum|record|struct|constant|annotation"
LANGUAGES = "python|javascript|typescript|go|java"


class Symbol(BaseModel):
    name: str
    kind: str
    container: str | None
    signature: str | None


class Citation(BaseModel):
    repository: str
    branch: str
    commit: str
    path: str
    line_start: int
    line_end: int
    blob_sha: str
    permalink: str


class CodeHit(BaseModel):
    symbol: Symbol
    language: str
    citation: Citation
    owners: list[str]
    behind: bool
    snapshot_status: str | None
    item: dict[str, str]


class CodeHitPage(BaseModel):
    items: list[CodeHit]
    next_cursor: str | None


class BranchSnapshot(BaseModel):
    repository: str
    repository_status: str
    branch: str
    head_commit: str | None
    indexed_commit: str | None
    status: str | None  # COMPLETE | PARTIAL | None (never indexed)
    indexing: bool  # a snapshot of the head is in progress
    behind: bool
    truncated: bool
    counts: dict[str, Any]
    skipped: list[dict[str, Any]]
    renames: list[dict[str, Any]]
    codeowners: dict[str, Any] | None
    finished_at: datetime | None


class CodeFile(BaseModel):
    citation: Citation
    language: str
    owners: list[str]
    behind: bool
    definitions: list[dict[str, Any]]
    imports: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


# ---------------------------------------------------------------- helpers


def _web_base(api_base: str) -> str:
    """https://api.github.com -> https://github.com; https://ghe.example/api/v3 -> https://ghe.example"""
    if api_base.rstrip("/") == "https://api.github.com":
        return "https://github.com"
    return api_base.rstrip("/").removesuffix("/api/v3")


class _Context:
    """Per-request lookups: latest snapshots, heads, owners, web bases (each loaded once)."""

    def __init__(self, conn: Connection, project_id: uuid.UUID):
        self.conn, self.project_id = conn, project_id
        self.repos = {
            r.id: r
            for r in conn.execute(
                select(project_repositories, source_connections.c.base_url)
                .join(
                    source_connections,
                    (source_connections.c.tenant_id == project_repositories.c.tenant_id)
                    & (source_connections.c.id == project_repositories.c.connection_id),
                )
                .where(project_repositories.c.project_id == project_id)
            )
        }
        latest: dict[tuple, dict] = {}
        running: set[tuple] = set()
        self.by_commit: dict[tuple, dict] = {}
        for (
            s
        ) in conn.execute(  # the latest FINISHED snapshot is the one a branch's files reflect (INPROOM-178)
            select(code_snapshots)
            .where(code_snapshots.c.project_id == project_id)
            .order_by(code_snapshots.c.finished_at.asc().nulls_first(), code_snapshots.c.started_at)
        ).mappings():
            key = (s["source_id"], s["branch"])
            self.by_commit[(s["source_id"], s["branch"], s["commit_sha"])] = dict(s)
            if s["status"] == "RUNNING":
                running.add((key, s["commit_sha"]))
            elif s["status"] != "RETIRED":
                latest[key] = dict(s)
        self.latest, self.running = latest, running
        self.heads = {
            r.source_id: dict((r.watermark or {}).get("heads") or {})
            for r in conn.execute(
                select(source_sync_state.c.source_id, source_sync_state.c.watermark).where(
                    source_sync_state.c.source_kind == "git",
                    source_sync_state.c.source_id.in_(list(self.repos)),
                )
            )
        }
        self._owners: dict[tuple, list] = {}

    def snapshot(self, source_id, branch) -> dict | None:
        return self.latest.get((source_id, branch))

    def head(self, source_id, branch) -> str | None:
        return self.heads.get(source_id, {}).get(branch)

    def behind(self, source_id, branch) -> bool:
        snap, head = self.snapshot(source_id, branch), self.head(source_id, branch)
        return snap is None or (head is not None and snap["commit_sha"] != head)

    def owners(self, source_id, branch, path: str, commit: str) -> list[str]:
        """Owners by CODEOWNERS AT `commit` - the commit the citation names (INPROOM-180)."""
        key = (source_id, branch, commit)
        if key not in self._owners:
            snap = self.by_commit.get(key)
            text = ((snap or {}).get("codeowners") or {}).get("text")
            self._owners[key] = parse(text)[0] if text else []
        return owners_of(self._owners[key], path)

    def behind_at(self, source_id, branch, commit: str) -> bool:
        """The branch head has moved past the commit this content is verified at."""
        head = self.head(source_id, branch)
        return head is not None and commit != head

    def citation(self, source_id, branch, path, commit, blob_sha, start, end) -> Citation:
        """`commit`: the item's verified_ref - a commit that contains exactly this content (INPROOM-177)."""
        repo = self.repos[source_id]
        commit = commit or ""
        # Each path segment is URL-encoded ("#", "?", "%", spaces, Unicode); "/" separators and the line
        # fragment stay literal, so the URL names exactly the git path (INPROOM-181).
        encoded = quote(path, safe="/")
        permalink = f"{_web_base(repo.base_url)}/{repo.full_name}/blob/{commit}/{encoded}#L{start}-L{end}"
        return Citation(
            repository=repo.full_name,
            branch=branch,
            commit=commit,
            path=path,
            line_start=start,
            line_end=end,
            blob_sha=blob_sha,
            permalink=permalink,
        )


def _encode(values: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")


def _decode(cursor: str) -> list:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if isinstance(value, list) and len(value) == 5:
            return value
    except ValueError:
        pass
    raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed")


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- routes


@router.get(
    "/v1/projects/{project_id}/code/search",
    response_model=CodeHitPage,
    summary="Find symbols by name (exact, then prefix, then contains); every hit is a commit-pinned citation",
    responses={400: PROBLEM, 404: PROBLEM},
)
def search(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    kind: Annotated[str | None, Query(pattern=f"^({KINDS})$")] = None,
    language: Annotated[str | None, Query(pattern=f"^({LANGUAGES})$")] = None,
    repository: Annotated[str | None, Query(max_length=200)] = None,
    branch: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=600)] = None,
) -> CodeHitPage:
    s, d, o, r = code_symbols, derivatives, source_objects, project_repositories
    lname, ql = func.lower(s.c.name), q.lower()
    rank = case((lname == ql, 0), (lname.like(_like(ql) + "%", escape="\\"), 1), else_=2)
    order = (rank, lname, s.c.path, s.c.line_start, s.c.id)
    stmt = (
        select(s, rank.label("rank"), lname.label("lname"), o.c.verified_ref)
        .join(d, d.c.id == s.c.derivative_id)
        .join(o, o.c.id == s.c.object_id)
        .join(r, r.c.id == s.c.source_id)
        .where(
            s.c.project_id == project_id,
            lname.like("%" + _like(ql) + "%", escape="\\"),
            d.c.status == "READY",
            d.c.purged_at.is_(None),
            o.c.current_revision_id == s.c.revision_id,
            o.c.status == "CURRENT",
            r.c.status == "ACTIVE",  # a paused or excluded repository's code is withheld
            s.c.branch == func.any(r.c.branches),  # and so is a branch no longer selected (INPROOM-179)
        )
    )
    if kind:
        stmt = stmt.where(s.c.kind == kind)
    if language:
        stmt = stmt.where(s.c.language == language)
    if repository:
        stmt = stmt.where(r.c.full_name == repository)
    if branch:
        stmt = stmt.where(s.c.branch == branch)
    if cursor:
        stmt = stmt.where(tuple_(*order) > tuple_(*_decode(cursor)))
    stmt = stmt.order_by(*order).limit(limit + 1)
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = [dict(x) for x in conn.execute(stmt).mappings()]
        ctx = _Context(conn, project_id) if rows else None
    more, rows = len(rows) > limit, rows[:limit]
    items = []
    for x in rows:
        snap = ctx.snapshot(x["source_id"], x["branch"])
        items.append(
            CodeHit(
                symbol=Symbol(
                    name=x["name"], kind=x["kind"], container=x["container"], signature=x["signature"]
                ),
                language=x["language"],
                citation=ctx.citation(
                    x["source_id"],
                    x["branch"],
                    x["path"],
                    x["verified_ref"] or x["commit_sha"],
                    x["blob_sha"],
                    x["line_start"],
                    x["line_end"],
                ),
                owners=ctx.owners(
                    x["source_id"], x["branch"], x["path"], x["verified_ref"] or x["commit_sha"]
                ),
                behind=ctx.behind_at(x["source_id"], x["branch"], x["verified_ref"] or x["commit_sha"]),
                snapshot_status=snap["status"] if snap else None,
                item={
                    "kind": "git",
                    "source_id": str(x["source_id"]),
                    "external_id": f"{x['branch']}:{x['path']}",
                },
            )
        )
    nxt = None
    if more:
        last = rows[-1]
        nxt = _encode([last["rank"], last["lname"], last["path"], last["line_start"], last["id"]])
    return CodeHitPage(items=items, next_cursor=nxt)


@router.get(
    "/v1/projects/{project_id}/code/snapshots",
    response_model=list[BranchSnapshot],
    summary="Per repository and branch: indexed commit vs head (behind), complete or partial, skips, renames",
    responses={404: PROBLEM},
)
def snapshots(request: Request, principal: CurrentPrincipal, project_id: uuid.UUID) -> list[BranchSnapshot]:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        ctx = _Context(conn, project_id)
    out = []
    for source_id, repo in sorted(ctx.repos.items(), key=lambda kv: kv[1].full_name):
        for branch in repo.branches:
            snap = ctx.snapshot(source_id, branch)
            head = ctx.head(source_id, branch)
            owners = (snap or {}).get("codeowners")
            out.append(
                BranchSnapshot(
                    repository=repo.full_name,
                    repository_status=repo.status,
                    branch=branch,
                    head_commit=head,
                    indexed_commit=snap["commit_sha"] if snap else None,
                    status=snap["status"] if snap else None,
                    indexing=((source_id, branch), head) in ctx.running,
                    behind=ctx.behind(source_id, branch),
                    truncated=bool(snap and snap["truncated"]),
                    counts=(snap or {}).get("counts") or {},
                    skipped=((snap or {}).get("skipped") or [])[:100],
                    renames=(snap or {}).get("renames") or [],
                    codeowners=(
                        {
                            "path": owners.get("path"),
                            "errors": parse(owners["text"])[1] if owners.get("text") else [],
                        }
                        if owners
                        else None
                    ),
                    finished_at=(snap or {}).get("finished_at"),
                )
            )
    return out


@router.get(
    "/v1/projects/{project_id}/code/files",
    response_model=CodeFile,
    summary="One file's symbols and imports at the indexed commit, with owners and its citation",
    responses={404: PROBLEM, 409: PROBLEM},
)
def file_symbols(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    repository: Annotated[str, Query(max_length=200)],
    branch: Annotated[str, Query(max_length=255)],
    path: Annotated[str, Query(min_length=1, max_length=1000)],
) -> CodeFile:
    from app.modules.content import _readable

    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        repo = (
            conn.execute(
                select(project_repositories).where(
                    project_repositories.c.project_id == project_id,
                    project_repositories.c.full_name == repository,
                )
            )
            .mappings()
            .first()
        )
        if repo is None:
            raise ApiError(404, "CODE_NOT_FOUND", "Not found", "No such repository in this project")
        if branch not in repo["branches"]:  # INPROOM-179: only selected branches are served
            raise ApiError(404, "CODE_NOT_FOUND", "Not found", "This branch is not selected for the project")
        obj = conn.execute(
            select(source_objects.c.revisions).where(
                source_objects.c.project_id == project_id,
                source_objects.c.source_kind == "git",
                source_objects.c.source_id == repo["id"],
                source_objects.c.external_id == f"{branch}:{path}",
            )
        ).first()
        if obj is None:
            raise ApiError(404, "CODE_NOT_FOUND", "Not found", "This file is not indexed on this branch")
        item, rev = _readable(
            conn, principal, request, project_id, "git", repo["id"], f"{branch}:{path}", obj.revisions
        )
        parsed = (
            conn.execute(
                select(derivatives)
                .where(derivatives.c.revision_id == rev["id"], derivatives.c.producer == "symbols")
                .order_by(derivatives.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if parsed is None or parsed["status"] != "READY":
            status = parsed["status"] if parsed else "NONE"
            error = (parsed or {}).get("error") or {}
            raise ApiError(
                409,
                "NOT_INDEXED",
                "Not indexed",
                f"symbols are {status.lower()}",
                parse_status=status,
                failure=error.get("code"),
            )
        doc = json.loads(blobs.read(blobs.get_store(), parsed["blob_key"]))
        ctx = _Context(conn, project_id)
    last = max((d["line_end"] for d in doc["definitions"]), default=1)
    commit = item.get("verified_ref") or (rev.get("acl") or {}).get("commit")
    return CodeFile(
        citation=ctx.citation(repo["id"], branch, path, commit, rev["provider_version"] or "", 1, last),
        language=doc["language"],
        owners=ctx.owners(repo["id"], branch, path, commit or ""),
        behind=ctx.behind_at(repo["id"], branch, commit or ""),
        definitions=doc["definitions"],
        imports=doc["imports"],
        warnings=doc["warnings"],
    )
