"""Resolve a parsed page's attachment references to stored attachment items (INPROOM-166 / INPROOM-28)."""

from typing import Any

from sqlalchemy import Connection, select

from app.db_schema import source_objects


def resolve_attachments(
    conn: Connection, obj: dict[str, Any], refs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Each {"filename", "section"} ref of the page `obj`, with the attachment item it names (by title, among
    the page's attachments) or item=None when it is not stored (unsupported, skipped, or not on this page)."""
    if not refs:
        return []
    items = {
        r.title: r
        for r in conn.execute(
            select(source_objects.c.external_id, source_objects.c.title, source_objects.c.status).where(
                source_objects.c.project_id == obj["project_id"],
                source_objects.c.source_kind == obj["source_kind"],
                source_objects.c.source_id == obj["source_id"],
                source_objects.c.parent_external_id == obj["external_id"],
            )
        )
    }
    out = []
    for ref in refs:
        item = items.get(ref["filename"])
        out.append(
            ref
            | {
                "item": None
                if item is None
                else {
                    "kind": obj["source_kind"],
                    "source_id": str(obj["source_id"]),
                    "external_id": item.external_id,
                    "status": item.status,
                }
            }
        )
    return out
