"""Project settings schema v2 and versioned settings API (INPROOM-102 / story INPROOM-21).

Every change is a new, immutable row in project_settings_versions (the app role can only INSERT/SELECT it).
Saves use optimistic concurrency on the settings version (If-Match), identical content is a no-op, and each
real change writes one audit event and one outbox event in the same transaction.

Production actions are off by default. Turning them on, or changing the production block while it is on,
requires tenant_admin (permission project.production.enable); turning them off is allowed to project admins.
"""

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection, insert, select, update
from sqlalchemy.exc import IntegrityError

from app import audit
from app.api_errors import ApiError, conflict, precondition_failed, precondition_required
from app.auth import CurrentPrincipal, Principal
from app.authz import authorize, require_tenant
from app.db import tenant_session
from app.db_schema import outbox_events, project_settings_versions, projects
from app.pagination import MAX_LIMIT, Page

router = APIRouter(tags=["settings"])
PROBLEM = {"description": "problem+json"}

SCHEMA_VERSION = 2
KNOWN_PROCESS_PACKAGES = ("sdlc@1", "stlc@1")
APPROVAL_SCOPES = ("requirement", "plan", "impact", "test", "release")
SOURCE_KINDS = ("git", "jira", "confluence", "upload")

ApprovalScope = Literal["requirement", "plan", "impact", "test", "release"]
SourceKind = Literal["git", "jira", "confluence", "upload"]


def _ordered(values: list[str], order: tuple[str, ...]) -> list[str]:
    """Canonical list form (deduplicated, fixed order) so equal settings always hash equally."""
    return [v for v in order if v in set(values)]


class Approvals(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_scopes: list[ApprovalScope] = Field(
        default_factory=lambda: ["plan"], description="Approvals required before any external write"
    )
    min_approvers: int = Field(default=1, ge=1, le=3)
    expiry_hours: int = Field(default=72, ge=1, le=168)
    self_approval: Literal[False] = Field(default=False, description="Never allowed")

    @field_validator("required_scopes")
    @classmethod
    def _canonical(cls, v: list[str]) -> list[str]:
        return _ordered(v, APPROVAL_SCOPES)


class Retention(BaseModel):
    """How long stored source content lives (INPROOM-160). Lineage metadata is always kept."""

    model_config = ConfigDict(extra="forbid")
    keep_revisions: int = Field(default=10, ge=1, le=100, description="Newest revisions always kept per item")
    superseded_days: int = Field(
        default=90, ge=1, le=3650, description="Older revisions are purged this long after being replaced"
    )
    deleted_days: int = Field(
        default=30, ge=1, le=3650, description="Content of deleted items is purged this long after deletion"
    )


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed_kinds: list[SourceKind] = Field(default_factory=lambda: list(SOURCE_KINDS), min_length=1)
    max_classification: Literal["public", "internal", "confidential"] = "internal"
    approve_new_sources: bool = True
    retention: Retention = Field(default_factory=Retention)

    @field_validator("allowed_kinds")
    @classmethod
    def _canonical(cls, v: list[str]) -> list[str]:
        return _ordered(v, SOURCE_KINDS)


class Production(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions_enabled: bool = False
    environments: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")]] = Field(
        default_factory=list, max_length=10
    )
    enabled_by: str | None = Field(default=None, description="Set by the server; client values are ignored")
    enabled_at: datetime | None = Field(
        default=None, description="Set by the server; client values are ignored"
    )

    @field_validator("environments")
    @classmethod
    def _canonical(cls, v: list[str]) -> list[str]:
        return sorted(set(v))


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[2] = SCHEMA_VERSION
    process_package: str = Field(default="sdlc@1", pattern=r"^[a-z][a-z0-9-]*@\d+$")
    write_mode: Literal["read_only", "draft_only", "approved_writes"] = "read_only"
    model_policy: Literal["tenant_default", "local_only", "cloud_allowed"] = "tenant_default"
    approvals: Approvals = Field(default_factory=Approvals)
    sources: Sources = Field(default_factory=Sources)
    production: Production = Field(default_factory=Production)


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: ProjectSettings
    change_note: str | None = Field(default=None, max_length=500)


class SettingsVersion(BaseModel):
    project_id: uuid.UUID
    version: int
    schema_version: int = Field(description="Schema the version was stored with (1 = legacy, read as v2)")
    settings: ProjectSettings
    content_sha256: str
    created_by: str
    created_at: datetime
    change_note: str | None
    changed_paths: list[str]


class SettingsChange(BaseModel):
    path: str
    old: Any = None
    new: Any = None


class SettingsVersionDetail(SettingsVersion):
    diff: list[SettingsChange] = Field(
        description="Changes against the previous version (empty for version 1)"
    )


# ---------------------------------------------------------------- pure helpers


def default_settings(process_package: str = "sdlc@1") -> ProjectSettings:
    return ProjectSettings(process_package=process_package)


def upgrade(stored: dict[str, Any]) -> ProjectSettings:
    """Read any stored settings as schema v2. Legacy v1 rows ({write_mode, approval_policy, model_policy,
    process_package}) are never rewritten; they are interpreted here with v2 defaults for new fields."""
    if stored.get("schema_version") == SCHEMA_VERSION:
        return ProjectSettings.model_validate(stored)
    fields = ProjectSettings.model_fields
    legacy: dict[str, Any] = {"process_package": stored.get("process_package") or "sdlc@1"}
    for name in ("write_mode", "model_policy"):
        allowed = fields[name].annotation.__args__  # type: ignore[union-attr]
        if stored.get(name) in allowed:
            legacy[name] = stored[name]
    # v1 "approval_required_for_external_writes" is exactly the v2 approvals default.
    return ProjectSettings.model_validate(legacy)


def canonical(settings: ProjectSettings) -> dict[str, Any]:
    return settings.model_dump(mode="json")


def content_hash(settings: ProjectSettings) -> str:
    raw = json.dumps(canonical(settings), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out |= _flatten(v, f"{prefix}.{k}" if prefix else k)
        return out
    return {prefix: value}


def diff(old: ProjectSettings | None, new: ProjectSettings) -> list[SettingsChange]:
    """Leaf-level changes as dotted paths (lists compared as a whole)."""
    before = _flatten(canonical(old)) if old else {}
    after = _flatten(canonical(new))
    return [
        SettingsChange(path=p, old=before.get(p), new=after.get(p))
        for p in sorted(before.keys() | after.keys())
        if before.get(p) != after.get(p)
    ]


def rule_errors(settings: ProjectSettings) -> list[dict[str, Any]]:
    """Cross-field rules, reported like request validation errors (loc + msg)."""
    errors = []

    def err(loc: tuple[str, ...], msg: str) -> None:
        errors.append({"loc": ["body", "settings", *loc], "msg": msg, "type": "settings_rule"})

    if settings.process_package not in KNOWN_PROCESS_PACKAGES:
        err(("process_package",), f"unknown process package; known: {', '.join(KNOWN_PROCESS_PACKAGES)}")
    if settings.write_mode == "approved_writes" and not settings.approvals.required_scopes:
        err(("approvals", "required_scopes"), "approved_writes needs at least one required approval scope")
    if settings.production.actions_enabled:
        if not settings.production.environments:
            err(("production", "environments"), "enabling production actions needs at least one environment")
        if "release" not in settings.approvals.required_scopes:
            err(
                ("approvals", "required_scopes"),
                "enabling production actions requires the release approval scope",
            )
    return errors


def _production_block(s: ProjectSettings) -> dict[str, Any]:
    return {"actions_enabled": s.production.actions_enabled, "environments": s.production.environments}


def needs_production_authority(old: ProjectSettings, new: ProjectSettings) -> bool:
    """Turning production on, or changing it while on, needs tenant_admin. Turning it off does not."""
    if not new.production.actions_enabled:
        return False
    return _production_block(old) != _production_block(new)


def stamp_production(old: ProjectSettings, new: ProjectSettings, principal: Principal) -> ProjectSettings:
    """enabled_by/enabled_at are owned by the server."""
    prod = new.production
    if not prod.actions_enabled:
        stamped = prod.model_copy(update={"enabled_by": None, "enabled_at": None})
    elif old.production.actions_enabled:
        stamped = prod.model_copy(
            update={"enabled_by": old.production.enabled_by, "enabled_at": old.production.enabled_at}
        )
    else:
        stamped = prod.model_copy(update={"enabled_by": principal.user_id, "enabled_at": datetime.now(UTC)})
    return new.model_copy(update={"production": stamped})


# ---------------------------------------------------------------- persistence helpers


def etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(value: str | None, what: str) -> int:
    if value is None:
        raise precondition_required(f"If-Match header with the current {what} version (ETag) is required")
    match = re.fullmatch(r'\s*(?:W/)?"?(\d+)"?\s*', value)
    if not match:
        raise precondition_failed("If-Match must be the ETag returned by the API")
    return int(match.group(1))


def _version_row(conn: Connection, tenant_id: uuid.UUID, project_id: uuid.UUID, version: int) -> dict | None:
    row = (
        conn.execute(
            select(project_settings_versions).where(
                project_settings_versions.c.tenant_id == tenant_id,
                project_settings_versions.c.project_id == project_id,
                project_settings_versions.c.version == version,
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _as_version(row: dict) -> SettingsVersion:
    return SettingsVersion(
        project_id=row["project_id"],
        version=row["version"],
        schema_version=row["schema_version"],
        settings=upgrade(row["settings"]),
        content_sha256=row["content_sha256"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        change_note=row["change_note"],
        changed_paths=list(row["changed_paths"] or []),
    )


def current_settings(conn: Connection, tenant_id: uuid.UUID, project: dict) -> SettingsVersion:
    row = _version_row(conn, tenant_id, project["id"], project["settings_version"])
    assert row is not None, "projects.settings_version points at a missing version"
    return _as_version(row)


def insert_version(
    conn: Connection,
    principal: Principal,
    project_id: uuid.UUID,
    version: int,
    settings: ProjectSettings,
    *,
    change_note: str | None = None,
    changed_paths: list[str] | None = None,
) -> None:
    conn.execute(
        insert(project_settings_versions).values(
            tenant_id=principal.tenant_id,
            project_id=project_id,
            version=version,
            schema_version=SCHEMA_VERSION,
            settings=canonical(settings),
            content_sha256=content_hash(settings),
            created_by=principal.user_id,
            change_note=change_note,
            changed_paths=changed_paths or [],
        )
    )


# ---------------------------------------------------------------- endpoints


@router.get(
    "/v1/projects/{project_id}/settings",
    response_model=SettingsVersion,
    summary="Current project settings (schema v2) with their version",
    responses={404: PROBLEM},
)
def get_settings_(request: Request, response: Response, principal: CurrentPrincipal, project_id: uuid.UUID):
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.read", project_id)
        current = current_settings(conn, principal.tenant_id, access.project)
    response.headers["ETag"] = etag(current.version)
    return current


@router.put(
    "/v1/projects/{project_id}/settings",
    response_model=SettingsVersion,
    summary="Save project settings as a new immutable version (If-Match = current settings version)",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 412: PROBLEM, 422: PROBLEM, 428: PROBLEM},
)
def put_settings(
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    payload: Annotated[SettingsUpdate, Body()],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    expected = parse_if_match(if_match, "settings")
    errors = rule_errors(payload.settings)
    if errors:
        raise ApiError(
            422, "SETTINGS_INVALID", "Invalid settings", "Settings break a project rule", errors=errors
        )
    with tenant_session(principal.tenant_id) as conn:
        access = authorize(conn, principal, request, "project.settings.update", project_id, lock=True)
        project = access.project
        if project["status"] == "ARCHIVED":
            raise conflict("PROJECT_ARCHIVED", "Archived projects are read-only; restore the project first")
        if project["settings_version"] != expected:
            raise precondition_failed(
                f"Settings were changed by someone else; current ETag is {etag(project['settings_version'])}"
            )
        current = current_settings(conn, principal.tenant_id, project)
        if needs_production_authority(current.settings, payload.settings):
            require_tenant(
                principal,
                "project.production.enable",
                request,
                target_type="project",
                target_id=str(project_id),
            )
        new = stamp_production(current.settings, payload.settings, principal)
        if content_hash(new) == content_hash(current.settings):
            response.headers["ETag"] = etag(current.version)
            return current  # identical content: no new version, audit or event
        from app.content import feed  # late: content -> modules -> settings

        withheld_before = feed.setting_withheld(conn, project_id)
        changes = diff(current.settings, new)
        version = current.version + 1
        paths = [c.path for c in changes]
        try:
            insert_version(
                conn,
                principal,
                project_id,
                version,
                new,
                change_note=payload.change_note,
                changed_paths=paths,
            )
        except IntegrityError as exc:  # defensive: the row lock makes this unreachable
            raise precondition_failed("Settings were changed by someone else; reload and retry") from exc
        conn.execute(
            update(projects)
            .where(projects.c.tenant_id == principal.tenant_id, projects.c.id == project_id)
            .values(settings_version=version)
        )
        # INPROOM-186: the cap or the allowed kinds may withhold or restore whole sources
        feed.append_setting_delta(
            conn, principal.tenant_id, project_id, withheld_before, feed.setting_withheld(conn, project_id)
        )
        audit.record(
            conn,
            principal,
            "project.settings.update",
            "project_settings",
            str(project_id),
            trace_id=request.state.trace_id,
            payload={
                "from_version": current.version,
                "to_version": version,
                "old_sha256": current.content_sha256,
                "new_sha256": content_hash(new),
                "changed_paths": paths,
                "change_note": payload.change_note,
            },
            project_id=project_id,
        )
        conn.execute(
            insert(outbox_events).values(
                tenant_id=principal.tenant_id,
                type="project.settings.changed",
                payload={
                    "project_id": str(project_id),
                    "version": version,
                    "changed_paths": paths,
                    "changed_by": principal.user_id,
                },
            )
        )
        saved = _as_version(_version_row(conn, principal.tenant_id, project_id, version) or {})
    response.headers["ETag"] = etag(version)
    return saved


def _encode(version: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([version]).encode()).decode().rstrip("=")


def _decode(cursor: str) -> int:
    try:
        [version] = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return int(version)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, "INVALID_CURSOR", "Invalid cursor", "cursor is malformed") from exc


@router.get(
    "/v1/projects/{project_id}/settings/versions",
    response_model=Page[SettingsVersion],
    summary="Settings history, newest first",
    responses={404: PROBLEM},
)
def list_versions(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    cursor: str | None = None,
) -> Page[SettingsVersion]:
    stmt = select(project_settings_versions).where(
        project_settings_versions.c.tenant_id == principal.tenant_id,
        project_settings_versions.c.project_id == project_id,
    )
    if cursor:
        stmt = stmt.where(project_settings_versions.c.version < _decode(cursor))
    stmt = stmt.order_by(project_settings_versions.c.version.desc()).limit(limit + 1)
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        rows = [dict(r) for r in conn.execute(stmt).mappings()]
    nxt = _encode(rows[limit - 1]["version"]) if len(rows) > limit else None
    return Page[SettingsVersion](items=[_as_version(r) for r in rows[:limit]], next_cursor=nxt)


@router.get(
    "/v1/projects/{project_id}/settings/versions/{version}",
    response_model=SettingsVersionDetail,
    summary="One settings version and its diff against the previous version",
    responses={404: PROBLEM},
)
def get_version(
    request: Request,
    principal: CurrentPrincipal,
    project_id: uuid.UUID,
    version: Annotated[int, Path(ge=1, le=1_000_000)],
) -> SettingsVersionDetail:
    with tenant_session(principal.tenant_id) as conn:
        authorize(conn, principal, request, "project.read", project_id)
        row = _version_row(conn, principal.tenant_id, project_id, version)
        if row is None:
            raise ApiError(
                404, "SETTINGS_VERSION_NOT_FOUND", "Not found", f"Settings version {version} does not exist"
            )
        previous = _version_row(conn, principal.tenant_id, project_id, version - 1) if version > 1 else None
    current = _as_version(row)
    before = upgrade(previous["settings"]) if previous else None
    changes = diff(before, current.settings) if before else []
    return SettingsVersionDetail(**current.model_dump(), diff=changes)
