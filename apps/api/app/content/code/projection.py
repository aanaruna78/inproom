"""code_symbols: the searchable projection of READY symbols derivatives (INPROOM-175 / story INPROOM-29).

Rows are written in the same transaction that marks a symbols derivative READY, and dropped whenever the
derivative leaves READY for good (superseded by a newer revision, invalidated, or purged). Search also applies
the live() rules, so a row can never outlive its revision's visibility even between those steps.
"""

from typing import Any

from sqlalchemy import Connection, delete, insert

from app.db_schema import code_symbols, knowledge_chunks


def project(
    conn: Connection,
    tenant,
    *,
    project_id,
    derivative_id,
    revision: dict[str, Any],
    obj: dict[str, Any],
    doc: dict[str, Any],
) -> int:
    branch, _, path = obj["external_id"].partition(":")
    commit = (revision.get("acl") or {}).get("commit") or ""
    rows = [
        {
            "tenant_id": tenant,
            "project_id": project_id,
            "derivative_id": derivative_id,
            "revision_id": revision["id"],
            "object_id": obj["id"],
            "source_id": obj["source_id"],
            "branch": branch,
            "path": path,
            "blob_sha": revision.get("provider_version") or "",
            "commit_sha": commit,
            "language": doc.get("language") or "",
            "kind": d["kind"],
            "name": d["name"],
            "container": d.get("container"),
            "signature": d.get("signature"),
            "line_start": d["line_start"],
            "line_end": d["line_end"],
        }
        for d in doc.get("definitions") or []
    ]
    if rows:
        conn.execute(insert(code_symbols), rows)
    return len(rows)


def drop(conn: Connection, derivative_ids: list) -> int:
    """Remove the queryable projections of derivatives that left READY: code symbols and, from INPROOM-203,
    retrieval chunks. Every caller (superseded, invalidated, purged) goes through here."""
    if not derivative_ids:
        return 0
    dropped = conn.execute(
        delete(code_symbols).where(code_symbols.c.derivative_id.in_(derivative_ids))
    ).rowcount
    dropped += conn.execute(
        delete(knowledge_chunks).where(knowledge_chunks.c.derivative_id.in_(derivative_ids))
    ).rowcount
    return dropped


def project_chunks(
    conn: Connection,
    tenant,
    *,
    project_id,
    derivative_id,
    revision: dict[str, Any],
    obj: dict[str, Any],
    doc: dict[str, Any],
) -> int:
    """INPROOM-203: one knowledge_chunks row per chunk of a READY chunks@1 derivative."""
    rows = [
        {
            "tenant_id": tenant,
            "project_id": project_id,
            "derivative_id": derivative_id,
            "revision_id": revision["id"],
            "object_id": obj["id"],
            "source_kind": obj["source_kind"],
            "source_id": obj["source_id"],
            "external_id": obj["external_id"],
            "seq": revision["seq"],
            "ordinal": c["ordinal"],
            "anchor": c.get("anchor"),
            "path": c.get("path") or [],
            "page": c.get("page"),
            "line_start": c.get("line_start"),
            "line_end": c.get("line_end"),
            "context": c.get("context") or "",
            "text": c["text"],
            "tokens": c["tokens"],
            "sha256": c["sha256"],
        }
        for c in doc.get("chunks") or []
    ]
    if rows:
        conn.execute(insert(knowledge_chunks), rows)
    return len(rows)
