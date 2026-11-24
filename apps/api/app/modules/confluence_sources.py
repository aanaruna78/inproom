"""Project Confluence sources: space selection with filters and the versioned catalog sync (INPROOM-135).

A project admin selects a space (optionally narrowed to page trees and required labels). Selection and
"sync now" list the space's current pages, plan the catalog changes (app.modules.confluence_sync) and apply
them
in one transaction under the project lock, so a failure part-way leaves the previous catalog intact.
Provider calls always run outside database transactions. INPROOM never writes to Confluence here.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, delete, insert, select, update

from app import audit
from app.api_errors import ApiError, conflict
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize
from app.connectors.base import NotFoundOrNoAccess, ProviderError
from app.connectors.confluence import ConfluenceProvider, PageListing, Space
from app.connectors.registry import Transports, confluence_for, provider_errors
from app.db import tenant_session
from app.db_schema import (
    confluence_page_versions,
    confluence_pages,
    outbox_events,
    project_confluence_sources,
    projects,
    source_connections,
)
from app.modules import settings as settings_module
from app.modules.confluence_sync import Filters, plan_sync
from app.modules.connections import credential_of, load_connection

router = APIRouter(tags=["confluence sources"])
PROBLEM = {"description": "problem+json"}
REVOKED_REASON = "connection revoked"
SPACE_REF = Annotated[
    str,
    Path(
        pattern=r"^[0-9a-fA-F-]{36}:~?[A-Za-z0-9_]{1,100}$",
        description="{connection_id}:{space key}, e.g. 4f1c...:ENG",
    ),
]
NOT_ACCESSIBLE = ("SPACE_NOT_ACCESSIBLE", "The space does not exist or the credential cannot read it")
LABEL = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\s,]+$")]
PAGE_ID = Annotated[str, Field(pattern=r"^\d{1,20}$")]


class SpaceFilters(BaseModel):
    root_page_ids: list[PAGE_ID] = Field(
        default_factory=list, max_length=20, description="Only these page trees"
    )
    required_labels: list[LABEL] = Field(
        default_factory=list, max_length=10, description="A page must carry every label (e.g. approved)"
    )
    include_restricted: bool = Field(
        default=False, description="Include pages with read restrictions (flagged; enforced by E03 ACLs)"
    )


class SpaceOption(BaseModel):
    id: str
    key: str
    name: str
    selected: bool


class SpaceOptions(BaseModel):
    items: list[SpaceOption]
    next_cursor: str | None


class ConfluenceSource(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    connection_id: uuid.UUID
    connection_name: str
    connection_status: str
    base_url: str
    space_id: str
    space_key: str
    name: str
    filters: dict[str, Any]
    capabilities: dict[str, Any]
    status: str
    status_reason: str | None
    warnings: list[dict[str, Any]]
    page_counts: dict[str, int]
    last_synced_at: datetime | None
    last_sync: dict[str, Any]
    selected_by: str
    selected_at: datetime
    updated_at: datetime
    version: int


class SyncResult(BaseModel):
    source: ConfluenceSource
    counts: dict[str, int]
    truncated: bool
    changed: bool


# ---------------------------------------------------------------- helpers


def _require_allowed(conn: Connection, principal: Principal, project: dict[str, Any]) -> str:
    current = settings_module.current_settings(conn, principal.tenant_id, project)
    if "confluence" not in current.settings.sources.allowed_kinds:
        raise conflict(
            "SOURCE_KIND_NOT_ALLOWED",
            "This project's settings do not allow confluence sources (sources.allowed_kinds)",
        )
    return current.settings.write_mode


def _parse_ref(ref: str) -> tuple[uuid.UUID, str]:
    connection, _, key = ref.partition(":")
    try:
        return uuid.UUID(connection), key
    except ValueError as exc:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Not found", "Unknown connection reference") from exc


def sources(conn: Connection, tenant_id: uuid.UUID, **where: Any) -> list[dict[str, Any]]:
    stmt = (
        select(
            project_confluence_sources,
            source_connections.c.display_name.label("connection_name"),
            source_connections.c.status.label("connection_status"),
            source_connections.c.base_url,
        )
        .join(
            source_connections,
            (source_connections.c.tenant_id == project_confluence_sources.c.tenant_id)
            & (source_connections.c.id == project_confluence_sources.c.connection_id),
        )
        .where(project_confluence_sources.c.tenant_id == tenant_id)
        .order_by(project_confluence_sources.c.space_key)
    )
    for column, value in where.items():
        stmt = stmt.where(project_confluence_sources.c[column] == value)
    rows = [dict(r) for r in conn.execute(stmt).mappings()]
    for row in rows:
        counts = conn.execute(
            select(confluence_pages.c.status, confluence_pages.c.page_id).where(
                confluence_pages.c.tenant_id == tenant_id, confluence_pages.c.source_id == row["id"]
            )
        ).all()
        row["page_counts"] = {
            s: sum(1 for c in counts if c.status == s)
            for s in ("CURRENT", "RESTRICTED", "EXCLUDED", "DELETED")
        }
    return rows


def source_of(conn: Connection, principal: Principal, project_id: uuid.UUID, source_id: uuid.UUID) -> dict:
    found = sources(conn, principal.tenant_id, project_id=project_id, id=source_id)
    if not found:
        raise ApiError(
            404, "SOURCE_NOT_FOUND", "Not found", "This Confluence space is not selected for the project"
        )
    return found[0]


def usable_connection(conn: Connection, principal: Principal, request: Request, connection_id: uuid.UUID):
    row = load_connection(conn, principal, request, connection_id, "project.source.manage")
    if row["status"] not in ("ACTIVE", "DEGRADED"):
        raise conflict("CONNECTION_NOT_ACTIVE", f"Connection is {row['status']}; check or rotate it first")
    return row, credential_of(conn, principal, row)


def _fetch(provider: ConfluenceProvider, key: str) -> tuple[Space, PageListing]:
    with provider_errors(NOT_ACCESSIBLE):
        space = provider.get_space(key)
        listing = provider.iter_pages(space)
    return space, listing


def _warnings(space: Space, write_mode: str, truncated: bool) -> list[dict[str, str]]:
    warnings = []
    if space.write_capable and write_mode == "read_only":
        warnings.append(
            {
                "code": "EXCESS_PRIVILEGE",
                "message": "credential can edit this space; the project is read_only and INPROOM only reads",
            }
        )
    if truncated:
        warnings.append(
            {
                "code": "SYNC_TRUNCATED",
                "message": "the space has more pages than one sync reads; narrow it with filters",
            }
        )
    return warnings


def _emit(
    conn: Connection,
    principal: Principal,
    trace_id: str | None,
    action: str,
    source: dict[str, Any],
    extra: dict[str, Any] | None = None,
    outbox: bool = True,
) -> None:
    payload = {
        "project_id": str(source["project_id"]),
        "connection_id": str(source["connection_id"]),
        "source_id": str(source["id"]),
        "space_key": source["space_key"],
    } | (extra or {})
    audit.record(
        conn,
        principal,
        action,
        "project_confluence_source",
        str(source["id"]),
        trace_id=trace_id,
        payload=payload,
        project_id=source["project_id"],
    )
    if outbox:
        conn.execute(
            insert(outbox_events).values(tenant_id=principal.tenant_id, type=action, payload=payload)
        )


def apply_sync(
    conn: Connection,
    principal: Principal,
    trace_id: str | None,
    source: dict[str, Any],
    space: Space,
    listing: PageListing,
    write_mode: str,
) -> tuple[dict[str, int], bool]:
    """Write the planned catalog changes (caller holds the project lock in this transaction)."""
    tenant = principal.tenant_id
    existing = {
        r["page_id"]: dict(r)
        for r in conn.execute(
            select(confluence_pages).where(
                confluence_pages.c.tenant_id == tenant, confluence_pages.c.source_id == source["id"]
            )
        ).mappings()
    }
    plan = plan_sync(existing, listing, Filters.of(source["filters"]))
    now = datetime.now(UTC)
    for row in plan.inserts:
        conn.execute(insert(confluence_pages).values(tenant_id=tenant, source_id=source["id"], **row))
    for row in plan.updates:
        values = {k: v for k, v in row.items() if k != "page_id"}
        conn.execute(
            update(confluence_pages)
            .where(
                confluence_pages.c.tenant_id == tenant,
                confluence_pages.c.source_id == source["id"],
                confluence_pages.c.page_id == row["page_id"],
            )
            .values(**values, updated_at=now)
        )
    seen = {
        (r.page_id, r.version)
        for r in conn.execute(
            select(confluence_page_versions.c.page_id, confluence_page_versions.c.version).where(
                confluence_page_versions.c.tenant_id == tenant,
                confluence_page_versions.c.source_id == source["id"],
            )
        )
    }
    for row in plan.versions:
        if (row["page_id"], row["version"]) not in seen:
            conn.execute(
                insert(confluence_page_versions).values(tenant_id=tenant, source_id=source["id"], **row)
            )
    base = {
        "project_id": str(source["project_id"]),
        "source_id": str(source["id"]),
        "space_key": source["space_key"],
    }
    for kind, payload in plan.events:
        conn.execute(insert(outbox_events).values(tenant_id=tenant, type=kind, payload=base | payload))
    summary = plan.counts | {"pages_listed": len(listing.pages)}
    conn.execute(
        update(project_confluence_sources)
        .where(
            project_confluence_sources.c.tenant_id == tenant, project_confluence_sources.c.id == source["id"]
        )
        .values(
            name=space.name,
            space_key=space.key,
            capabilities={"read": True, "write_capable": space.write_capable},
            warnings=_warnings(space, write_mode, listing.truncated),
            status="ACTIVE",
            status_reason=None,
            last_synced_at=now,
            last_sync=summary | {"truncated": listing.truncated},
            updated_at=now,
        )
    )
    _emit(
        conn,
        principal,
        trace_id,
        "source.confluence.synced",
        source,
        {"counts": plan.counts, "truncated": listing.truncated},
        outbox=False,
    )
    return plan.counts, plan.changed


# ---------------------------------------------------------------- routes


@router.get(
    "/v1/connections/{connection_id}/confluence/spaces",
    response_model=SpaceOptions,
    summary="Confluence spaces the connection's credential can read, for selecting into a project",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def list_space_options(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    connection_id: uuid.UUID,
    project_id: Annotated[uuid.UUID, Query(description="Project the spaces would be selected into")],
    q: Annotated[str | None, Query(max_length=100)] = None,
    cursor: Annotated[
        str | None, Query(max_length=2000, description="next_cursor of the previous page")
    ] = None,
) -> SpaceOptions:
    with tenant_session(principal.tenant_id) as conn:
        row = load_connection(conn, principal, request, connection_id, "project.source.manage")
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        _require_allowed(conn, principal, access.project)
        token = credential_of(conn, principal, row)
        chosen = {
            s["space_id"]
            for s in sources(conn, principal.tenant_id, project_id=project_id, connection_id=connection_id)
        }
    provider = confluence_for(row, token, transports)
    with provider_errors():
        try:
            found, nxt = provider.list_spaces(q, cursor)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed") from exc
    items = [SpaceOption(id=s.id, key=s.key, name=s.name, selected=s.id in chosen) for s in found]
    return SpaceOptions(items=items, next_cursor=nxt)


@router.get(
    "/v1/projects/{project_id}/sources/confluence",
    response_model=list[ConfluenceSource],
    summary="Confluence spaces selected for this project, with filters, page counts and last sync",
    responses={404: PROBLEM},
)
def list_confluence_sources(
    request: Request, principal: CurrentPrincipal, project_id: uuid.UUID
) -> list[ConfluenceSource]:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        return [
            ConfluenceSource.model_validate(s)
            for s in sources(conn, principal.tenant_id, project_id=project_id)
        ]


@router.put(
    "/v1/projects/{project_id}/sources/confluence/{ref}",
    response_model=SyncResult,
    summary="Select a Confluence space (or change its filters) and sync its page catalog; idempotent",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def select_space(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    project_id: uuid.UUID,
    ref: SPACE_REF,
    payload: Annotated[SpaceFilters | None, Body()] = None,
) -> SyncResult:
    connection_id, key = _parse_ref(ref)
    filters = Filters.of((payload or SpaceFilters()).model_dump())
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        _require_allowed(conn, principal, access.project)
        row, token = usable_connection(conn, principal, request, connection_id)
    space, listing = _fetch(confluence_for(row, token, transports), key)
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        write_mode = _require_allowed(conn, principal, access.project)
        found = sources(
            conn, principal.tenant_id, project_id=project_id, connection_id=connection_id, space_id=space.id
        )
        if not found:
            source_id = conn.execute(
                insert(project_confluence_sources)
                .values(
                    tenant_id=principal.tenant_id,
                    project_id=project_id,
                    connection_id=connection_id,
                    space_id=space.id,
                    space_key=space.key,
                    name=space.name,
                    filters=filters.as_dict(),
                    status="ACTIVE",
                    selected_by=principal.user_id,
                )
                .returning(project_confluence_sources.c.id)
            ).scalar_one()
            source = sources(conn, principal.tenant_id, id=source_id)[0]
            _emit(
                conn,
                principal,
                request.state.trace_id,
                "source.confluence.selected",
                source,
                {"filters": filters.as_dict()},
            )
        else:
            source = found[0]
            if Filters.of(source["filters"]) != filters:
                conn.execute(
                    update(project_confluence_sources)
                    .where(
                        project_confluence_sources.c.tenant_id == principal.tenant_id,
                        project_confluence_sources.c.id == source["id"],
                    )
                    .values(filters=filters.as_dict(), version=project_confluence_sources.c.version + 1)
                )
                source["filters"] = filters.as_dict()
                _emit(
                    conn,
                    principal,
                    request.state.trace_id,
                    "source.confluence.updated",
                    source,
                    {"filters": filters.as_dict()},
                )
        counts, changed = apply_sync(
            conn, principal, request.state.trace_id, source, space, listing, write_mode
        )
        updated = sources(conn, principal.tenant_id, id=source["id"])[0]
    return SyncResult(
        source=ConfluenceSource.model_validate(updated),
        counts=counts,
        truncated=listing.truncated,
        changed=changed,
    )


@router.delete(
    "/v1/projects/{project_id}/sources/confluence/{ref}",
    status_code=204,
    summary="Remove a Confluence space from this project (its catalog goes with it)",
    responses={403: PROBLEM, 404: PROBLEM},
)
def remove_space(
    request: Request, principal: CurrentPrincipal, project_id: uuid.UUID, ref: SPACE_REF
) -> Response:
    connection_id, key = _parse_ref(ref)
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            # Archived projects are read-only: deselecting would delete the source's catalog/history.
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        found = sources(
            conn, principal.tenant_id, project_id=project_id, connection_id=connection_id, space_key=key
        )
        if not found:
            raise ApiError(404, "SOURCE_NOT_FOUND", "Not found", "This space is not selected for the project")
        conn.execute(
            delete(project_confluence_sources).where(
                project_confluence_sources.c.tenant_id == principal.tenant_id,
                project_confluence_sources.c.id == found[0]["id"],
            )
        )
        _emit(conn, principal, request.state.trace_id, "source.confluence.removed", found[0])
    return Response(status_code=204)


@router.post(
    "/v1/projects/{project_id}/sources/confluence/{source_id}/sync",
    response_model=SyncResult,
    summary="Sync the space's page catalog now (versions, restrictions, deletions); unchanged = no writes",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def sync_now(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
) -> SyncResult:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        source = source_of(conn, principal, project_id, source_id)
        row, token = usable_connection(conn, principal, request, source["connection_id"])
    space, listing = _fetch(confluence_for(row, token, transports), source["space_key"])
    if space.id != source["space_id"]:
        raise conflict("SPACE_CHANGED", "The space key now points at a different space; reselect it")
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        write_mode = _require_allowed(conn, principal, access.project)
        source = source_of(conn, principal, project_id, source_id)
        counts, changed = apply_sync(
            conn, principal, request.state.trace_id, source, space, listing, write_mode
        )
        updated = source_of(conn, principal, project_id, source_id)
    return SyncResult(
        source=ConfluenceSource.model_validate(updated),
        counts=counts,
        truncated=listing.truncated,
        changed=changed,
    )


# ---------------------------------------------------------------- connection health (called by /check)


def evaluate_confluence(
    principal: Principal, connection: dict[str, Any], provider: ConfluenceProvider
) -> tuple[str, list[dict]]:
    """Can the credential still read every selected space? Lost spaces are paused (they leave the sync scope)
    and degrade the connection; recovery re-activates them. Write access on read_only projects warns."""
    with tenant_session(principal.tenant_id) as conn:
        rows = sources(conn, principal.tenant_id, connection_id=connection["id"])
        modes = {}
        for r in rows:
            project = conn.execute(select(projects).where(projects.c.id == r["project_id"])).mappings().one()
            modes[r["project_id"]] = settings_module.current_settings(
                conn, principal.tenant_id, dict(project)
            ).settings.write_mode
    issues: list[dict[str, Any]] = []
    outcomes: dict[Any, tuple[str, str | None]] = {}
    for row in rows:
        try:
            space = provider.get_space(row["space_key"])
        except NotFoundOrNoAccess:
            reason = "the credential can no longer read this space"
            issues.append({"code": "SPACE_NOT_ACCESSIBLE", "space": row["space_key"], "message": reason})
            outcomes[row["id"]] = ("PAUSED", reason)
            continue
        except ProviderError as exc:
            issues.append(
                {
                    "code": "PROVIDER_UNAVAILABLE",
                    "space": row["space_key"],
                    "message": str(exc),
                    "severity": "warning",
                }
            )
            continue
        outcomes[row["id"]] = ("ACTIVE", None)
        if space.write_capable and modes[row["project_id"]] == "read_only":
            issues.append(
                {
                    "code": "EXCESS_PRIVILEGE",
                    "space": row["space_key"],
                    "message": "credential can edit this space; the INPROOM project is read_only",
                    "severity": "warning",
                }
            )
    with tenant_session(principal.tenant_id) as conn:
        for row in rows:
            if row["id"] not in outcomes or row["status_reason"] == REVOKED_REASON:
                continue
            status, reason = outcomes[row["id"]]
            if (row["status"], row["status_reason"]) != (status, reason):
                conn.execute(
                    update(project_confluence_sources)
                    .where(
                        project_confluence_sources.c.tenant_id == principal.tenant_id,
                        project_confluence_sources.c.id == row["id"],
                    )
                    .values(
                        status=status,
                        status_reason=reason,
                        updated_at=datetime.now(UTC),
                        version=project_confluence_sources.c.version + 1,
                    )
                )
    degraded = any(i.get("severity") != "warning" for i in issues)
    return ("DEGRADED" if degraded else "ACTIVE"), issues
