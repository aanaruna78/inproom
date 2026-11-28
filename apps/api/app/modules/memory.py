"""Markdown project memory API (INPROOM-225 / story INPROOM-35). The generator is app/content/memory.py.

- `POST …/memory/snapshots` generates a snapshot from visible current knowledge. When its bytes equal the
  latest version's, that version is returned (200) instead of a duplicate; otherwise version n+1 is stored
  with its lines in one transaction (201) and audited. Creations of one project are serialized.
- Reads apply the visibility rule AGAIN: a stored line whose items (`depends_on`) are no longer visible is
  withheld (counted), so an old snapshot never shows content that was restricted, deleted, paused or put
  over the classification cap after it was taken. `served_sha256` is the digest of what was served.
"""

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import Connection, insert, select

from app import audit
from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal
from app.authz import authorize
from app.db import tenant_session
from app.db_schema import memory_snapshot_entries, memory_snapshots, projects, source_objects

router = APIRouter(tags=["memory"])
log = logging.getLogger(__name__)
PROBLEM = {"description": "problem+json"}


class CreateRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class SnapshotInfo(BaseModel):
    version: int
    created_at: datetime
    created_by: str
    reason: str
    generator: str
    feed_head: int  # the knowledge feed head it was generated at
    sha256: str  # of the Markdown body as generated
    counts: dict[str, int]  # entries per section, items not asserted, unconfirmed links, lines
    redactions: dict[str, int]  # secrets removed, by type (never the secrets)
    metadata: dict  # facts that vary without content changing (for example sync times): not in the body


class CreateResult(SnapshotInfo):
    created: bool  # false: identical to the latest version, which is returned


class Citation(BaseModel):
    kind: str
    source_id: uuid.UUID
    external_id: str
    seq: int
    label: str


class SnapshotDetail(SnapshotInfo):
    markdown: str  # as served: lines of items no longer visible are withheld
    served_sha256: str  # equals sha256 when nothing is withheld
    withheld: int  # lines withheld because an item they name is no longer visible
    citations: list[Citation]  # every citation of the served lines, in order


class SnapshotList(BaseModel):
    items: list[SnapshotInfo]


def _info(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row[k] for k in SnapshotInfo.model_fields}


def _get(conn: Connection, project_id: uuid.UUID, version: int) -> dict[str, Any]:
    row = (
        conn.execute(
            select(memory_snapshots).where(
                memory_snapshots.c.project_id == project_id, memory_snapshots.c.version == version
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApiError(404, "MEMORY_SNAPSHOT_NOT_FOUND", "Snapshot not found", f"No version {version}")
    return dict(row)


def served_lines(conn: Connection, project_id: uuid.UUID, snapshot_id: int) -> tuple[list[dict], int]:
    """The snapshot's lines in order, without lines naming an item that is no longer visible."""
    from app.content.visibility import visible

    e = memory_snapshot_entries
    rows = [
        dict(r)
        for r in conn.execute(
            select(e.c.section, e.c.line, e.c.citation, e.c.depends_on)
            .where(e.c.snapshot_id == snapshot_id)
            .order_by(e.c.ordinal)
        ).mappings()
    ]
    named = {o for r in rows for o in r["depends_on"] or []}
    shown: set = set()
    if named:
        shown = set(
            conn.execute(
                select(source_objects.c.id).where(
                    source_objects.c.id.in_(list(named)), visible(conn, project_id)
                )
            ).scalars()
        )
    out = [r for r in rows if all(o in shown for o in r["depends_on"] or [])]
    return out, len(rows) - len(out)


def _detail(conn: Connection, project_id: uuid.UUID, row: dict[str, Any]) -> SnapshotDetail:
    from app.content.memory import markdown_of, sha256_of

    lines, withheld = served_lines(conn, project_id, row["id"])
    md = markdown_of(lines)
    return SnapshotDetail(
        **_info(row),
        markdown=md,
        served_sha256=sha256_of(md),
        withheld=withheld,
        citations=[Citation(**x["citation"]) for x in lines if x["citation"]],
    )


@router.post(
    "/v1/projects/{project_id}/memory/snapshots",
    status_code=201,
    response_model=CreateResult,
    summary="Generate a Markdown memory snapshot; identical bytes return the latest version (no duplicate)",
    responses={200: {"description": "identical to the latest version"}, 404: PROBLEM, 409: PROBLEM},
)
def create_snapshot(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[CreateRequest, Body()],
) -> JSONResponse:
    from app.content.memory import GENERATOR, generate

    started = time.monotonic()
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.memory.publish", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        # one creation per project at a time: versions are gap-free and a concurrent retry finds the first
        conn.execute(select(projects.c.id).where(projects.c.id == project_id).with_for_update())
        snap = generate(conn, project_id)
        s = memory_snapshots
        latest = (
            conn.execute(select(s).where(s.c.project_id == project_id).order_by(s.c.version.desc()).limit(1))
            .mappings()
            .first()
        )
        if latest is not None and latest["sha256"] == snap.sha256 and latest["generator"] == GENERATOR:
            body = CreateResult(**_info(dict(latest)), created=False)
            status = 200
        else:
            version = (latest["version"] if latest else 0) + 1
            row = (
                conn.execute(
                    insert(s)
                    .values(
                        tenant_id=principal.tenant_id,
                        project_id=project_id,
                        version=version,
                        feed_head=snap.feed_head,
                        generator=GENERATOR,
                        sha256=snap.sha256,
                        counts=snap.counts,
                        redactions=snap.redactions,
                        metadata=snap.metadata,
                        reason=payload.reason,
                        created_by=principal.user_id,
                    )
                    .returning(*s.c)
                )
                .mappings()
                .one()
            )
            conn.execute(
                insert(memory_snapshot_entries),
                [
                    {
                        "tenant_id": principal.tenant_id,
                        "project_id": project_id,
                        "snapshot_id": row["id"],
                        "ordinal": i,
                        **line,
                    }
                    for i, line in enumerate(snap.lines)
                ],
            )
            audit.record(
                conn,
                principal,
                "memory.snapshot.created",
                "memory_snapshot",
                f"{project_id}:{version}",
                trace_id=getattr(request.state, "trace_id", None),
                payload={"version": version, "sha256": snap.sha256, "reason": payload.reason},
                project_id=project_id,
            )
            body = CreateResult(**_info(dict(row)), created=True)
            status = 201
    log.info(
        "memory snapshot project=%s %s",
        project_id,
        json.dumps(
            {
                "version": body.version,
                "created": body.created,
                "bytes": len(snap.markdown.encode()),
                "counts": snap.counts,
                "redactions": snap.redactions,
                "ms": round((time.monotonic() - started) * 1000),
            }
        ),
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


@router.get(
    "/v1/projects/{project_id}/memory/snapshots",
    response_model=SnapshotList,
    summary="Memory snapshot versions, newest first",
    responses={404: PROBLEM},
)
def list_snapshots(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SnapshotList:
    s = memory_snapshots
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = conn.execute(
            select(s).where(s.c.project_id == project_id).order_by(s.c.version.desc()).limit(limit)
        ).mappings()
        return SnapshotList(items=[SnapshotInfo(**_info(dict(r))) for r in rows])


@router.get(
    "/v1/projects/{project_id}/memory/snapshots/{version}",
    response_model=SnapshotDetail,
    summary="A memory snapshot as JSON, or as text/markdown with ?format=markdown; hidden items withheld",
    responses={200: {"content": {"text/markdown": {"schema": {"type": "string"}}}}, 404: PROBLEM},
)
def get_snapshot(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    version: int,
    format: Literal["json", "markdown"] = "json",
):
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        detail = _detail(conn, project_id, _get(conn, project_id, version))
    if format == "markdown":
        headers = {"X-Memory-Withheld": str(detail.withheld), "ETag": f'"{detail.served_sha256}"'}
        return PlainTextResponse(detail.markdown, media_type="text/markdown; charset=utf-8", headers=headers)
    return detail


# ---------------------------------------------------------------- diff and verify (INPROOM-226)


class SectionChange(BaseModel):
    section: str
    added: int
    removed: int


class DiffResult(BaseModel):
    from_version: int  # `against`
    to_version: int
    added: int
    removed: int
    sections: list[SectionChange]  # sections with changes, in document order
    unified: str  # a unified diff of the served Markdown (from -> to)


class VerifyResult(BaseModel):
    version: int
    reproducible: bool  # regenerating now gives exactly this version's bytes (same generator)
    sha256: str  # the version's, as generated
    current_sha256: str  # regenerated now
    generator: str
    current_generator: str
    withheld: int  # lines of this version now withheld (items no longer visible)
    drift: str  # unified diff from this version (as served) to the regenerated document; empty when equal


def diff_lines(old: list[dict], new: list[dict], old_name: str, new_name: str) -> dict[str, Any]:
    """Unified diff and per-section counts between two line lists. Pure (unit-tested)."""
    import difflib

    from app.content.memory import SECTIONS

    a = [(x["section"], x["line"]) for x in old]
    b = [(x["section"], x["line"]) for x in new]
    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            for section, _ in a[i1:i2]:
                removed[section] = removed.get(section, 0) + 1
        if tag in ("replace", "insert"):
            for section, _ in b[j1:j2]:
                added[section] = added.get(section, 0) + 1
    order = ["header", *(key for key, _, _ in SECTIONS)]
    changed = [s for s in order if added.get(s) or removed.get(s)]
    unified = "".join(
        difflib.unified_diff(
            [line + "\n" for _, line in a], [line + "\n" for _, line in b], fromfile=old_name, tofile=new_name
        )
    )
    return {
        "added": sum(added.values()),
        "removed": sum(removed.values()),
        "sections": [
            SectionChange(section=s, added=added.get(s, 0), removed=removed.get(s, 0)) for s in changed
        ],
        "unified": unified,
    }


@router.get(
    "/v1/projects/{project_id}/memory/snapshots/{version}/diff",
    response_model=DiffResult,
    summary="What changed between two memory snapshot versions (as served: hidden items withheld)",
    responses={404: PROBLEM, 422: PROBLEM},
)
def diff_snapshots(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    version: int,
    against: Annotated[int, Query(ge=1)],
) -> DiffResult:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        old = _get(conn, project_id, against)
        new = _get(conn, project_id, version)
        old_lines, _ = served_lines(conn, project_id, old["id"])
        new_lines, _ = served_lines(conn, project_id, new["id"])
    result = diff_lines(old_lines, new_lines, f"memory v{against}", f"memory v{version}")
    return DiffResult(from_version=against, to_version=version, **result)


@router.post(
    "/v1/projects/{project_id}/memory/snapshots/{version}/verify",
    response_model=VerifyResult,
    summary="Regenerate from current knowledge and compare with a version: reproducible, or the drift",
    responses={404: PROBLEM},
)
def verify_snapshot(
    request: Request, principal: CurrentPrincipal, project_id: uuid.UUID, version: int
) -> VerifyResult:
    """Read-only: nothing is stored. A version is reproducible when today's knowledge renders exactly its
    bytes with the same generator; otherwise `drift` shows what changed since (content, visibility, or a
    newer generator)."""
    from app.content.memory import GENERATOR, generate

    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        row = _get(conn, project_id, version)
        stored, withheld = served_lines(conn, project_id, row["id"])
        current = generate(conn, project_id)
    reproducible = current.sha256 == row["sha256"] and row["generator"] == GENERATOR
    drift = (
        ""
        if reproducible
        else diff_lines(stored, current.lines, f"memory v{version}", "regenerated")["unified"]
    )
    return VerifyResult(
        version=version,
        reproducible=reproducible,
        sha256=row["sha256"],
        current_sha256=current.sha256,
        generator=row["generator"],
        current_generator=GENERATOR,
        withheld=withheld,
        drift=drift,
    )
