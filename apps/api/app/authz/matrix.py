"""Render the role matrix from app.authz.policy as markdown (docs/security/rbac-matrix.md)."""

from app.authz import policy


def render() -> str:
    roles = policy.PROJECT_ROLES
    lines = [
        "# INPROOM role and permission matrix",
        "",
        "<!-- GENERATED from apps/api/app/authz/policy.py by `make rbac-matrix`. Do not edit by hand. -->",
        "",
        "## Tenant roles (from the identity token)",
        "",
        "| Tenant role | Tenant permissions | Inside every project |",
        "|---|---|---|",
        "| (every signed-in user) | "
        + ", ".join(sorted(policy.TENANT_MEMBER_PERMISSIONS))
        + " | needs membership |",
    ]
    for role in policy.TENANT_ROLES:
        perms = policy.TENANT_ROLE_PERMISSIONS[role]
        tenant = "all tenant permissions" if "*" in perms else ", ".join(sorted(perms))
        inside = policy.TENANT_ROLE_PROJECT_GRANTS.get(role) or policy.TENANT_ROLE_PROJECT_PERMISSIONS.get(
            role
        )
        label = (
            "acts as " + ", ".join(sorted(policy.TENANT_ROLE_PROJECT_GRANTS[role]))
            if role in policy.TENANT_ROLE_PROJECT_GRANTS
            else ", ".join(sorted(inside or []))
        )
        lines.append(f"| {role} | {tenant} | {label} |")
    lines += [
        "",
        "Project role names in the token are ignored: project access comes only from project membership,",
        "checked in the database on every request, so removal and downgrades take effect immediately.",
        "",
        "**auditor is a read-only cap:** an auditor keeps only "
        + ", ".join(sorted(policy.READ_ONLY_CAP[policy.AUDITOR]))
        + " on every project, even if they also hold write-capable project roles.",
        "tenant_admin overrides the cap.",
        "",
        "## Project roles (from project membership)",
        "",
        "| Permission | Kind | " + " | ".join(roles) + " |",
        "|---|---|" + "---|" * len(roles),
    ]
    for perm, kind in policy.PROJECT_PERMISSIONS.items():
        cells = []
        for role in roles:
            if role not in policy.PROJECT_MATRIX[perm]:
                cells.append("")
            elif perm == "approval.decide":
                scopes = policy.approval_scopes(frozenset(), frozenset({role}))
                cells.append("any scope" if role == policy.PROJECT_ADMIN else ", ".join(sorted(scopes)))
            else:
                cells.append("✓")
        lines.append(f"| {perm} | {kind.value} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Approval scopes",
        "",
        "| Scope | May decide |",
        "|---|---|",
    ]
    for scope, allowed in policy.APPROVER_SCOPES.items():
        lines.append(f"| {scope} | {', '.join(sorted(allowed | {policy.PROJECT_ADMIN}))} |")
    lines += [
        "",
        "The proposer of an artifact can never decide its approval (`SELF_APPROVAL_FORBIDDEN`), whatever",
        "their role - including project_admin and tenant_admin.",
        "",
        "## Responses",
        "",
        "| Situation | Response | Audit |",
        "|---|---|---|",
        "| Project missing, in another tenant, or caller not a member | 404 NOT_FOUND (identical) | DENIED |",
        "| Member whose roles lack the permission or approval scope "
        "| 403 FORBIDDEN + required_permission | DENIED |",
        "| Proposer deciding their own approval | 403 SELF_APPROVAL_FORBIDDEN | DENIED |",
        "",
    ]
    return "\n".join(lines)
