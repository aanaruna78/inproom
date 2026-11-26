"""The knowledge change feed (INPROOM-186 / story INPROOM-31).

The invalidation contract:
1. The commit that changes an item's or a source's state also withholds it from every read path. There is no
   window in which a removed item is still served.
2. That same commit appends one entry per affected scope to the project's ordered, gap-free feed
   (`knowledge_changes`), plus one outbox event `knowledge.changed` {project_id, from_seq} per transaction.
   Consumers that keep copies (retrieval indexes, caches) resume from their last seq and drop or rebuild.

Most entries are written by database triggers (migration 0020), so no code path can change state without
them. The item status changes are into or out of CURRENT, a new current revision, and retention purging
bytes. The source status changes are into or out of ACTIVE, removal, and git branches deselected or selected.
Only changes driven by project settings, which no single row change expresses, are appended from here:
`setting_withheld` before and after, then `append_setting_delta`.
"""

import uuid
from typing import Any

from sqlalchemy import Connection, select, text

from app.db_schema import knowledge_changes, knowledge_heads

REASONS = {
    "DELETED": "the item was deleted at the source, or is no longer visible to INPROOM",
    "EXCLUDED": "the item no longer matches the source's filters (moved, label removed, filters changed)",
    "RESTRICTED": "the item carries read restrictions at the source",
    "TOO_LARGE": "the item is larger than the content size limit",
    "PURGED": "retention deleted the stored bytes of this revision",
    "NEW_REVISION": "the item has new content",
    "SOURCE_PAUSED": "the source is paused (access lost, or paused by a user)",
    "SOURCE_REMOVED": "the source was removed from the project",
    "BRANCH_DESELECTED": "the branch is no longer selected for the repository",
    "BRANCH_SELECTED": "the branch is selected again",
    "UPLOADS_DISABLED": "uploads are not an allowed source kind in the project settings",
    "CLASSIFICATION_EXCEEDS_PROJECT": "the source is classified above the project's maximum classification",
}


def append(
    conn: Connection,
    tenant: uuid.UUID,
    project_id: uuid.UUID,
    change: str,
    kind: str,
    source_id: uuid.UUID,
    reason: str,
    external_id: str | None = None,
    branch: str | None = None,
) -> int:
    """Append one entry (item scope when external_id is given) and return its seq."""
    return conn.execute(
        text("SELECT knowledge_append(:t, :p, :c, :scope, :k, :s, :e, :b, :r, NULL)"),
        {
            "t": tenant,
            "p": project_id,
            "c": change,
            "scope": "item" if external_id is not None else "source",
            "k": kind,
            "s": source_id,
            "e": external_id,
            "b": branch,
            "r": reason,
        },
    ).scalar_one()


def setting_withheld(conn: Connection, project_id: uuid.UUID) -> dict[tuple[str, uuid.UUID], str]:
    """Sources withheld because of project settings or policies: {(kind, source_id): reason}."""
    from app.content import policies
    from app.modules.content import _source_status

    withheld = {key: "CLASSIFICATION_EXCEEDS_PROJECT" for key in policies.over_cap(conn, project_id)}
    if _source_status(conn, {"source_kind": "upload", "project_id": project_id}) == "DISABLED":
        withheld.setdefault(("upload", project_id), "UPLOADS_DISABLED")
    return withheld


def append_setting_delta(
    conn: Connection,
    tenant: uuid.UUID,
    project_id: uuid.UUID,
    before: dict[tuple[str, uuid.UUID], str],
    after: dict[tuple[str, uuid.UUID], str],
) -> int:
    """Source-scope entries for what a settings or policy change withheld or restored. Returns the count."""
    n = 0
    for key in sorted(set(before) | set(after), key=lambda k: (k[0], str(k[1]))):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if old:  # restored, or withheld now for a different reason (both are recorded)
            append(conn, tenant, project_id, "restored", key[0], key[1], old)
            n += 1
        if new:
            append(conn, tenant, project_id, "invalidated", key[0], key[1], new)
            n += 1
    return n


def changes(
    conn: Connection, project_id: uuid.UUID, after: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Entries with seq > after, in order, and the project's head seq."""
    k = knowledge_changes
    rows = [
        dict(r)
        for r in conn.execute(
            select(k).where(k.c.project_id == project_id, k.c.seq > after).order_by(k.c.seq).limit(limit)
        ).mappings()
    ]
    head = conn.execute(
        select(knowledge_heads.c.seq).where(knowledge_heads.c.project_id == project_id)
    ).scalar()
    return rows, int(head or 0)
