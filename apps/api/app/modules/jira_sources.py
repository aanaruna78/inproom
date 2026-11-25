"""Project Jira sources: selection, schema discovery and mapping (INPROOM-131 / story INPROOM-23).

A project admin selects Jira projects from a tenant Jira connection. Selection runs schema discovery; each
distinct schema is stored once as an immutable, versioned snapshot with a diff against the previous one.
The mapping into INPROOM's model is derived from the snapshot plus the admin's overrides. Provider calls run
outside database transactions; writes happen afterwards under the project lock. INPROOM never writes to Jira
here: write permissions are only discovered and reported (excess privilege on read_only projects).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, delete, insert, or_, select, update

from app import audit
from app.api_errors import ApiError, conflict, precondition_failed
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize
from app.connectors.base import NotFoundOrNoAccess, ProviderError
from app.connectors.jira import READ_PERMISSION, WRITE_PERMISSIONS, JiraProvider
from app.connectors.registry import Transports, jira_for, provider_errors
from app.db import tenant_session
from app.db_schema import (
    jira_schema_snapshots,
    outbox_events,
    project_jira_sources,
    projects,
    source_connections,
)
from app.modules import jira_schema
from app.modules import settings as settings_module
from app.modules.connections import credential_of, load_connection

router = APIRouter(tags=["jira sources"])
PROBLEM = {"description": "problem+json"}
REVOKED_REASON = "connection revoked"
HELD = ("excluded", "connection_revoked")  # pauses health checks never undo (INPROOM-145)
JIRA_REF = Annotated[
    str,
    Path(
        pattern=r"^[0-9a-fA-F-]{36}:[A-Z][A-Z0-9_]{0,19}$",
        description="{connection_id}:{jira project key}, e.g. 4f1c...:ABC",
    ),
]


def _parse_ref(ref: str) -> tuple[uuid.UUID, str]:
    connection, _, key = ref.partition(":")
    try:
        return uuid.UUID(connection), key
    except ValueError as exc:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Not found", "Unknown connection reference") from exc


NOT_ACCESSIBLE = (
    "JIRA_PROJECT_NOT_ACCESSIBLE",
    "The Jira project does not exist or the credential cannot browse it",
)


class JiraProjectOption(BaseModel):
    id: str
    key: str
    name: str
    selected: bool


class JiraProjectOptions(BaseModel):
    items: list[JiraProjectOption]
    next_page: int | None


class JiraSource(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    connection_id: uuid.UUID
    connection_name: str
    connection_status: str
    base_url: str
    jira_project_id: str
    jira_key: str
    name: str
    status: str
    status_reason: str | None
    capabilities: dict[str, Any]
    mapping: dict[str, Any]
    mapping_version: int
    snapshot_version: int
    warnings: list[dict[str, Any]]
    selected_by: str
    selected_at: datetime
    updated_at: datetime
    version: int


class DiscoveryResult(BaseModel):
    source: JiraSource
    changed: bool
    snapshot_version: int
    diff: dict[str, Any]


class SchemaSnapshot(BaseModel):
    source_id: uuid.UUID
    version: int
    fingerprint: str
    schema_: dict[str, Any] = Field(alias="schema", serialization_alias="schema")
    diff: dict[str, Any]
    discovered_by: str
    discovered_at: datetime

    model_config = {"populate_by_name": True}


CanonicalField = Literal["story_points", "sprint", "epic_link", "acceptance_criteria"]
IssueTypeKind = Literal["epic", "story", "task", "subtask", "bug", "other"]
StatusKind = Literal["todo", "in_progress", "done"]
SOURCE_ID = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$")]


class MappingOverrides(BaseModel):
    """Replaces the overrides. A field mapped to null means "do not read this canonical field"."""

    fields: dict[CanonicalField, SOURCE_ID | None] = Field(default_factory=dict)
    issue_types: dict[SOURCE_ID, IssueTypeKind] = Field(default_factory=dict, max_length=200)
    statuses: dict[SOURCE_ID, StatusKind] = Field(default_factory=dict, max_length=500)


# ---------------------------------------------------------------- helpers


def _require_jira_allowed(conn: Connection, principal: Principal, project: dict[str, Any]) -> str:
    current = settings_module.current_settings(conn, principal.tenant_id, project)
    if "jira" not in current.settings.sources.allowed_kinds:
        raise conflict(
            "SOURCE_KIND_NOT_ALLOWED",
            "This project's settings do not allow jira sources (sources.allowed_kinds)",
        )
    return current.settings.write_mode


def _write_mode(conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID) -> str:
    project = conn.execute(select(projects).where(projects.c.id == project_id)).mappings().one()
    return settings_module.current_settings(conn, tenant_id, dict(project)).settings.write_mode


def _sources(conn: Connection, tenant_id: uuid.UUID, **where: Any) -> list[dict[str, Any]]:
    stmt = (
        select(
            project_jira_sources,
            source_connections.c.display_name.label("connection_name"),
            source_connections.c.status.label("connection_status"),
            source_connections.c.base_url,
        )
        .join(
            source_connections,
            (source_connections.c.tenant_id == project_jira_sources.c.tenant_id)
            & (source_connections.c.id == project_jira_sources.c.connection_id),
        )
        .where(project_jira_sources.c.tenant_id == tenant_id)
        .order_by(project_jira_sources.c.jira_key)
    )
    for column, value in where.items():
        stmt = stmt.where(project_jira_sources.c[column] == value)
    return [dict(r) for r in conn.execute(stmt).mappings()]


def _source(
    conn: Connection, principal: Principal, project_id: uuid.UUID, source_id: uuid.UUID
) -> dict[str, Any]:
    found = _sources(conn, principal.tenant_id, project_id=project_id, id=source_id)
    if not found:
        raise ApiError(
            404, "SOURCE_NOT_FOUND", "Not found", "This Jira source is not selected for the project"
        )
    return found[0]


def _snapshot(
    conn: Connection, tenant_id: uuid.UUID, source_id: uuid.UUID, version: int
) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(jira_schema_snapshots).where(
                jira_schema_snapshots.c.tenant_id == tenant_id,
                jira_schema_snapshots.c.source_id == source_id,
                jira_schema_snapshots.c.version == version,
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _warnings(mapping_warnings: list[dict], capabilities: dict[str, Any], write_mode: str) -> list[dict]:
    warnings = list(mapping_warnings)
    if capabilities.get("write_capable") and write_mode == "read_only":
        granted = [p for p in WRITE_PERMISSIONS if capabilities["permissions"].get(p)]
        warnings.append(
            {
                "code": "EXCESS_PRIVILEGE",
                "message": f"credential can write ({', '.join(granted)}) but the project is read_only; "
                "INPROOM only needs Browse here",
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
    outbox: str | None = None,
) -> None:
    payload = {
        "project_id": str(source["project_id"]),
        "connection_id": str(source["connection_id"]),
        "source_id": str(source["id"]),
        "jira_key": source["jira_key"],
    } | (extra or {})
    audit.record(
        conn,
        principal,
        action,
        "project_jira_source",
        str(source["id"]),
        trace_id=trace_id,
        payload=payload,
        project_id=source["project_id"],
    )
    if outbox:
        conn.execute(
            insert(outbox_events).values(tenant_id=principal.tenant_id, type=outbox, payload=payload)
        )


def _apply_discovery(
    conn: Connection,
    principal: Principal,
    source: dict[str, Any],
    schema: dict[str, Any],
    capabilities: dict[str, Any],
    write_mode: str,
) -> tuple[bool, dict[str, Any]]:
    """Store a new snapshot if the schema changed; always refresh mapping, capabilities and warnings."""
    latest = _snapshot(conn, principal.tenant_id, source["id"], source["snapshot_version"])
    print_ = jira_schema.fingerprint(schema)
    changed = latest is None or latest["fingerprint"] != print_
    change = jira_schema.diff(latest["schema"] if latest else None, schema)
    version = source["snapshot_version"]
    if changed:
        version += 1
        conn.execute(
            insert(jira_schema_snapshots).values(
                tenant_id=principal.tenant_id,
                source_id=source["id"],
                version=version,
                fingerprint=print_,
                schema=schema,
                diff=change,
                discovered_by=principal.user_id,
            )
        )
    mapping, mapping_warnings = jira_schema.build_mapping(
        schema, (source.get("mapping") or {}).get("overrides")
    )
    values = {
        "name": schema["project"]["name"],
        "jira_key": schema["project"]["key"],
        "capabilities": capabilities,
        "mapping": mapping,
        "snapshot_version": version,
        "warnings": _warnings(mapping_warnings, capabilities, write_mode),
        "status": "ACTIVE",
        "status_reason": None,
        "pause_cause": None,
    }
    if source.get("pause_cause") in HELD:
        # an excluded (or revoked) source keeps its pause; discovery still refreshes schema and mapping
        for key in ("status", "status_reason", "pause_cause"):
            values.pop(key)
    if changed or any(source.get(k) != v for k, v in values.items()):
        conn.execute(
            update(project_jira_sources)
            .where(
                project_jira_sources.c.tenant_id == principal.tenant_id,
                project_jira_sources.c.id == source["id"],
            )
            .values(**values, updated_at=datetime.now(UTC), version=project_jira_sources.c.version + 1)
        )
    return changed, change


def _discover(provider: JiraProvider, key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with provider_errors(NOT_ACCESSIBLE):
        schema, capabilities = jira_schema.discover(provider, key)
    if not capabilities["read"]:
        raise ApiError(
            422, NOT_ACCESSIBLE[0], "Unprocessable request", "The credential lacks Browse permission"
        )
    return schema, capabilities


def _connection_for_use(conn: Connection, principal: Principal, request: Request, connection_id: uuid.UUID):
    row = load_connection(conn, principal, request, connection_id, "project.source.manage")
    if row["status"] not in ("ACTIVE", "DEGRADED"):
        raise conflict("CONNECTION_NOT_ACTIVE", f"Connection is {row['status']}; check or rotate it first")
    return row, credential_of(conn, principal, row)


# ---------------------------------------------------------------- routes


@router.get(
    "/v1/connections/{connection_id}/jira/projects",
    response_model=JiraProjectOptions,
    summary="Jira projects the connection's credential can browse, for selecting into a project",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def list_jira_project_options(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    connection_id: uuid.UUID,
    project_id: Annotated[uuid.UUID, Query(description="Project the Jira projects would be selected into")],
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1, le=100)] = 1,
) -> JiraProjectOptions:
    with tenant_session(principal.tenant_id) as conn:
        row = load_connection(conn, principal, request, connection_id, "project.source.manage")
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        _require_jira_allowed(conn, principal, access.project)
        token = credential_of(conn, principal, row)
        chosen = {
            s["jira_project_id"]
            for s in _sources(conn, principal.tenant_id, project_id=project_id, connection_id=connection_id)
        }
    provider = jira_for(row, token, transports)
    with provider_errors():
        found, more = provider.list_projects(q, page)
    items = [JiraProjectOption(id=p.id, key=p.key, name=p.name, selected=p.id in chosen) for p in found]
    return JiraProjectOptions(items=items, next_page=page + 1 if more else None)


@router.get(
    "/v1/projects/{project_id}/sources/jira",
    response_model=list[JiraSource],
    summary="Jira projects selected for this project, with capabilities, mapping and warnings",
    responses={404: PROBLEM},
)
def list_jira_sources(
    request: Request, principal: CurrentPrincipal, project_id: uuid.UUID
) -> list[JiraSource]:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        return [
            JiraSource.model_validate(s) for s in _sources(conn, principal.tenant_id, project_id=project_id)
        ]


@router.put(
    "/v1/projects/{project_id}/sources/jira/{ref}",
    response_model=JiraSource,
    summary="Select a Jira project for this project and discover its schema (idempotent)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def select_jira_project(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    project_id: uuid.UUID,
    ref: JIRA_REF,
) -> JiraSource:
    connection_id, jira_key = _parse_ref(ref)
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        _require_jira_allowed(conn, principal, access.project)
        row, token = _connection_for_use(conn, principal, request, connection_id)
    schema, capabilities = _discover(jira_for(row, token, transports), jira_key)
    jira_project = schema["project"]
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        write_mode = _require_jira_allowed(conn, principal, access.project)
        existing = _sources(
            conn,
            principal.tenant_id,
            project_id=project_id,
            connection_id=connection_id,
            jira_project_id=jira_project["id"],
        )
        if existing:
            source = existing[0]
            was_paused = source["status"] != "ACTIVE"
            changed, change = _apply_discovery(conn, principal, source, schema, capabilities, write_mode)
            if changed or was_paused:
                _emit(
                    conn,
                    principal,
                    request.state.trace_id,
                    "source.jira.discovered",
                    source,
                    {"changed": changed, "diff": change},
                    "source.jira.schema.changed" if changed else None,
                )
        else:
            source_id = conn.execute(
                insert(project_jira_sources)
                .values(
                    tenant_id=principal.tenant_id,
                    project_id=project_id,
                    connection_id=connection_id,
                    jira_project_id=jira_project["id"],
                    jira_key=jira_project["key"],
                    name=jira_project["name"],
                    status="ACTIVE",
                    selected_by=principal.user_id,
                )
                .returning(project_jira_sources.c.id)
            ).scalar_one()
            source = _sources(conn, principal.tenant_id, id=source_id)[0]
            _apply_discovery(conn, principal, source, schema, capabilities, write_mode)
            _emit(
                conn,
                principal,
                request.state.trace_id,
                "source.jira.selected",
                source,
                {"write_capable": capabilities["write_capable"]},
                "source.jira.selected",
            )
        return JiraSource.model_validate(_sources(conn, principal.tenant_id, id=source["id"])[0])


@router.delete(
    "/v1/projects/{project_id}/sources/jira/{ref}",
    status_code=204,
    summary="Remove a Jira project from this project (its snapshots go with it)",
    responses={403: PROBLEM, 404: PROBLEM},
)
def remove_jira_project(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    ref: JIRA_REF,
) -> Response:
    connection_id, jira_key = _parse_ref(ref)
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            # Archived projects are read-only: deselecting would delete the source's catalog/history.
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        found = _sources(
            conn, principal.tenant_id, project_id=project_id, connection_id=connection_id, jira_key=jira_key
        )
        if not found:
            raise ApiError(
                404, "SOURCE_NOT_FOUND", "Not found", "This Jira project is not selected for the project"
            )
        conn.execute(
            delete(project_jira_sources).where(
                project_jira_sources.c.tenant_id == principal.tenant_id,
                project_jira_sources.c.id == found[0]["id"],
            )
        )
        _emit(
            conn,
            principal,
            request.state.trace_id,
            "source.jira.removed",
            found[0],
            outbox="source.jira.removed",
        )
    return Response(status_code=204)


@router.post(
    "/v1/projects/{project_id}/sources/jira/{source_id}/discover",
    response_model=DiscoveryResult,
    summary="Rediscover the Jira schema; a new snapshot (with diff) is stored only if it changed",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
def rediscover(
    request: Request,
    principal: CurrentPrincipal,
    transports: Transports,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
) -> DiscoveryResult:
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        source = _source(conn, principal, project_id, source_id)
        row, token = _connection_for_use(conn, principal, request, source["connection_id"])
    schema, capabilities = _discover(jira_for(row, token, transports), source["jira_project_id"])
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        write_mode = _require_jira_allowed(conn, principal, access.project)
        source = _source(conn, principal, project_id, source_id)
        changed, change = _apply_discovery(conn, principal, source, schema, capabilities, write_mode)
        _emit(
            conn,
            principal,
            request.state.trace_id,
            "source.jira.discovered",
            source,
            {"changed": changed, "diff": change},
            "source.jira.schema.changed" if changed else None,
        )
        updated = _source(conn, principal, project_id, source_id)
    return DiscoveryResult(
        source=JiraSource.model_validate(updated),
        changed=changed,
        snapshot_version=updated["snapshot_version"],
        diff=change if changed else {},
    )


@router.get(
    "/v1/projects/{project_id}/sources/jira/{source_id}/schema",
    response_model=SchemaSnapshot,
    summary="A discovered schema snapshot (latest by default) with its diff against the previous one",
    responses={404: PROBLEM},
)
def get_schema(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> SchemaSnapshot:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        source = _source(conn, principal, project_id, source_id)
        snap = _snapshot(conn, principal.tenant_id, source_id, version or source["snapshot_version"])
    if snap is None:
        raise ApiError(404, "SNAPSHOT_NOT_FOUND", "Not found", "No such schema snapshot version")
    return SchemaSnapshot.model_validate(snap)


@router.put(
    "/v1/projects/{project_id}/sources/jira/{source_id}/mapping",
    response_model=JiraSource,
    summary="Override the mapping (If-Match = current mapping version); targets must exist in the snapshot",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 412: PROBLEM, 422: PROBLEM, 428: PROBLEM},
)
def update_mapping(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    payload: Annotated[MappingOverrides, Body()],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JiraSource:
    expected = settings_module.parse_if_match(if_match, "mapping")
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.source.manage", project_id, lock=True)
        if access.project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        write_mode = _require_jira_allowed(conn, principal, access.project)
        source = _source(conn, principal, project_id, source_id)
        if source["mapping_version"] != expected:
            raise precondition_failed(
                f"The mapping was changed by someone else; current ETag is "
                f"{settings_module.etag(source['mapping_version'])}"
            )
        snap = _snapshot(conn, principal.tenant_id, source_id, source["snapshot_version"])
        overrides = payload.model_dump(mode="json")
        errors = jira_schema.override_errors(snap["schema"], overrides)
        if errors:
            raise ApiError(
                422, "MAPPING_TARGET_UNKNOWN", "Unprocessable request", "; ".join(errors), errors=errors
            )
        mapping, mapping_warnings = jira_schema.build_mapping(snap["schema"], overrides)
        if mapping != source["mapping"]:
            conn.execute(
                update(project_jira_sources)
                .where(
                    project_jira_sources.c.tenant_id == principal.tenant_id,
                    project_jira_sources.c.id == source_id,
                )
                .values(
                    mapping=mapping,
                    warnings=_warnings(mapping_warnings, source["capabilities"], write_mode),
                    mapping_version=project_jira_sources.c.mapping_version + 1,
                    updated_at=datetime.now(UTC),
                    version=project_jira_sources.c.version + 1,
                )
            )
            _emit(
                conn,
                principal,
                request.state.trace_id,
                "source.jira.mapping.updated",
                source,
                {"overrides": overrides, "mapping_version": source["mapping_version"] + 1},
                "source.jira.mapping.updated",
            )
        updated = _source(conn, principal, project_id, source_id)
    response.headers["ETag"] = settings_module.etag(updated["mapping_version"])
    return JiraSource.model_validate(updated)


# ---------------------------------------------------------------- connection health (called by /check)


def evaluate_jira(
    principal: Principal, connection: dict[str, Any], provider: JiraProvider
) -> tuple[str, list[dict]]:
    """Can the credential still browse every selected Jira project? Lost ones are paused (they leave the sync
    scope) and degrade the connection; recovery re-activates them. Write access on read_only projects is an
    EXCESS_PRIVILEGE warning. Provider calls first, outside any transaction; results applied in one."""
    with tenant_session(principal.tenant_id) as conn:
        rows = _sources(conn, principal.tenant_id, connection_id=connection["id"])
        modes = {r["project_id"]: _write_mode(conn, principal.tenant_id, r["project_id"]) for r in rows}
    issues: list[dict[str, Any]] = []
    outcomes: dict[Any, tuple[str, str | None, dict | None]] = {}
    for row in rows:
        try:
            permissions = provider.my_permissions(row["jira_key"])
        except NotFoundOrNoAccess:
            permissions = {}
        except ProviderError as exc:
            issues.append(
                {
                    "code": "PROVIDER_UNAVAILABLE",
                    "project": row["jira_key"],
                    "message": str(exc),
                    "severity": "warning",
                }
            )
            continue
        if not permissions.get(READ_PERMISSION):
            reason = "the credential can no longer browse this Jira project"
            issues.append(
                {"code": "JIRA_PROJECT_NOT_ACCESSIBLE", "project": row["jira_key"], "message": reason}
            )
            outcomes[row["id"]] = ("PAUSED", reason, None)
            continue
        write = {p: permissions.get(p, False) for p in WRITE_PERMISSIONS}
        capabilities = {
            "read": True,
            "write_capable": any(write.values()),
            "permissions": {READ_PERMISSION: True} | write,
        }
        outcomes[row["id"]] = ("ACTIVE", None, capabilities)
        if capabilities["write_capable"] and modes[row["project_id"]] == "read_only":
            issues.append(
                {
                    "code": "EXCESS_PRIVILEGE",
                    "project": row["jira_key"],
                    "message": "credential can write to this Jira project; the INPROOM project is read_only",
                    "severity": "warning",
                }
            )
    with tenant_session(principal.tenant_id) as conn:
        for row in rows:
            if row["id"] not in outcomes or row["pause_cause"] in HELD:
                continue  # unknown (provider outage), or only a new connection re-activates revoked sources
            status, reason, capabilities = outcomes[row["id"]]
            values: dict[str, Any] = {
                "status": status,
                "status_reason": reason,
                "pause_cause": "access" if status == "PAUSED" else None,
            }
            if capabilities is not None:
                values["capabilities"] = capabilities
            if any(row[k] != v for k, v in values.items()):
                conn.execute(
                    update(project_jira_sources)
                    .where(
                        project_jira_sources.c.tenant_id == principal.tenant_id,
                        project_jira_sources.c.id == row["id"],
                        # INPROOM-149: re-check at write time. A person may have excluded the source (or the
                        # connection been revoked) while the provider calls ran outside this transaction:
                        # their pause then wins and this conditional update simply matches no row.
                        or_(
                            project_jira_sources.c.pause_cause.is_(None),
                            project_jira_sources.c.pause_cause.notin_(HELD),
                        ),
                    )
                    .values(
                        **values, updated_at=datetime.now(UTC), version=project_jira_sources.c.version + 1
                    )
                )
    degraded = any(i.get("severity") != "warning" for i in issues)
    return ("DEGRADED" if degraded else "ACTIVE"), issues
