"""Source trust and classification (INPROOM-183 / story INPROOM-30).

Every project source has an effective policy: its explicit row in source_policies, or the defaults by kind.
Classification is ENFORCED against the project's `sources.max_classification` setting: a source classified
above the cap is withheld everywhere content is served (raw, documents, code files, provenance, code search,
derivatives.live()) with reason CLASSIFICATION_EXCEEDS_PROJECT. Raising the cap or lowering the source's
classification restores access at once - nothing is deleted.
"""

import uuid
from typing import Any

from sqlalchemy import Connection, select

from app.db_schema import (
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    projects,
    source_policies,
)

TIERS = ("authoritative", "trusted", "reference", "untrusted")
CLASSIFICATIONS = ("public", "internal", "confidential")
RANK = {c: i for i, c in enumerate(CLASSIFICATIONS)}
DEFAULT_TRUST = {"jira": "trusted", "git": "trusted", "confluence": "trusted", "upload": "reference"}
DEFAULT_CLASSIFICATION = "internal"
SOURCE_TABLES = {
    "confluence": project_confluence_sources,
    "jira": project_jira_sources,
    "git": project_repositories,
}


def default(kind: str) -> dict[str, Any]:
    return {
        "tier": DEFAULT_TRUST.get(kind, "reference"),
        "classification": DEFAULT_CLASSIFICATION,
        "origin": "default",
        "reason": None,
        "version": 0,
        "updated_by": None,
        "updated_at": None,
    }


def policy_of(conn: Connection, kind: str, source_id) -> dict[str, Any]:
    row = (
        conn.execute(
            select(source_policies).where(
                source_policies.c.source_kind == kind, source_policies.c.source_id == source_id
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return default(kind)
    return {
        "tier": row["trust"],
        "classification": row["classification"],
        "origin": "policy",
        "reason": row["reason"],
        "version": row["version"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def project_cap(conn: Connection, project_id: uuid.UUID) -> str:
    from app.modules import settings as settings_module

    project = conn.execute(select(projects).where(projects.c.id == project_id)).mappings().first()
    if project is None:
        return DEFAULT_CLASSIFICATION
    return settings_module.current_settings(
        conn, project["tenant_id"], dict(project)
    ).settings.sources.max_classification


def exceeds(conn: Connection, kind: str, source_id, project_id) -> tuple[str, str] | None:
    """(classification, cap) when the source is classified above the project's cap, else None."""
    classification = policy_of(conn, kind, source_id)["classification"]
    cap = project_cap(conn, project_id)
    return (classification, cap) if RANK[classification] > RANK[cap] else None


def over_cap(conn: Connection, project_id: uuid.UUID) -> set[tuple[str, uuid.UUID]]:
    """Every (kind, source_id) of the project whose effective classification is above the cap."""
    cap = RANK[project_cap(conn, project_id)]
    explicit = {
        (r.source_kind, r.source_id): r.classification
        for r in conn.execute(
            select(
                source_policies.c.source_kind, source_policies.c.source_id, source_policies.c.classification
            ).where(source_policies.c.project_id == project_id)
        )
    }
    sources = [("upload", project_id)]
    for kind, table in SOURCE_TABLES.items():
        sources += [
            (kind, sid) for (sid,) in conn.execute(select(table.c.id).where(table.c.project_id == project_id))
        ]
    return {s for s in sources if RANK[explicit.get(s, DEFAULT_CLASSIFICATION)] > cap}
