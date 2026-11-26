"""Knowledge change feed API (INPROOM-186 / story INPROOM-31).

`GET /v1/projects/{id}/knowledge/changes?after=&limit=` returns what became unservable (`invalidated`),
servable again (`restored`) or got new content (`refreshed`), in order, from the cursor `after` (a seq).
Entries carry identifiers only: consumers that hold copies (indexes, caches) drop or rebuild by them. See
app/content/feed.py for the invalidation contract.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.auth import CurrentPrincipal
from app.authz import authorize
from app.content import feed
from app.db import tenant_session

router = APIRouter(tags=["knowledge"])
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
