"""Repository-level health for a connection (INPROOM-109/110).

For every repository selected (in any project of the tenant) through this connection, ask the provider
whether the credential can still read it and whether each selected branch still exists. Lost access or a
missing branch pauses that repository (it leaves the sync scope) and degrades the connection; recovery
re-activates it. Write-level permissions are reported as an EXCESS_PRIVILEGE warning (least privilege).
Provider calls happen first, outside any transaction; the results are then applied in one transaction.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from app.auth import Principal
from app.connectors.base import GitProvider, NotFoundOrNoAccess
from app.db import tenant_session
from app.db_schema import project_repositories

REVOKED_REASON = "connection revoked"
# Pauses that automatic health checks must never undo (INPROOM-145): only a person / adoption may.
HELD = ("excluded", "connection_revoked")


def evaluate(
    principal: Principal, connection: dict[str, Any], provider: GitProvider
) -> tuple[str, list[dict]]:
    with tenant_session(principal.tenant_id) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                select(project_repositories).where(
                    project_repositories.c.tenant_id == principal.tenant_id,
                    project_repositories.c.connection_id == connection["id"],
                )
            ).mappings()
        ]
    issues: list[dict[str, Any]] = []
    outcomes: dict[Any, tuple[str, str | None, dict[str, bool] | None]] = {}
    for row in rows:
        try:
            repo = provider.get_repository(row["provider_repo_id"])
        except NotFoundOrNoAccess:
            repo = None
        if repo is None or not repo.can_read:
            reason = "the credential can no longer read this repository"
            issues.append(
                {"code": "REPOSITORY_NOT_ACCESSIBLE", "repository": row["full_name"], "message": reason}
            )
            outcomes[row["id"]] = ("PAUSED", reason, None)
            continue
        missing = [b for b in row["branches"] if not provider.branch_exists(row["provider_repo_id"], b)]
        if missing:
            reason = f"branch not found: {', '.join(missing)}"
            issues.append({"code": "BRANCH_NOT_FOUND", "repository": row["full_name"], "message": reason})
            outcomes[row["id"]] = ("PAUSED", reason, repo.permissions)
        else:
            outcomes[row["id"]] = ("ACTIVE", None, repo.permissions)
        if repo.excess_privileges:
            issues.append(
                {
                    "code": "EXCESS_PRIVILEGE",
                    "repository": row["full_name"],
                    "message": f"credential has write-level access ({', '.join(repo.excess_privileges)}); "
                    "INPROOM only needs read access",
                    "severity": "warning",
                }
            )
    with tenant_session(principal.tenant_id) as conn:
        for row in rows:
            status, reason, permissions = outcomes[row["id"]]
            if row["pause_cause"] in HELD:
                continue  # excluded by a person / revoked connection: health never re-activates these
            values: dict[str, Any] = {
                "status": status,
                "status_reason": reason,
                "pause_cause": "access" if status == "PAUSED" else None,
            }
            if permissions is not None:
                values["permissions"] = permissions
            if (row["status"], row["status_reason"], row["permissions"], row["pause_cause"]) != (
                status,
                reason,
                permissions or row["permissions"],
                values["pause_cause"],
            ):
                conn.execute(
                    update(project_repositories)
                    .where(
                        project_repositories.c.tenant_id == principal.tenant_id,
                        project_repositories.c.id == row["id"],
                    )
                    .values(
                        **values, updated_at=datetime.now(UTC), version=project_repositories.c.version + 1
                    )
                )
    degraded = any(i.get("severity") != "warning" for i in issues)
    return ("DEGRADED" if degraded else "ACTIVE"), issues
