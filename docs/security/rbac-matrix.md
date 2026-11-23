# INPROOM role and permission matrix

<!-- GENERATED from apps/api/app/authz/policy.py by `make rbac-matrix`. Do not edit by hand. -->

## Tenant roles (from the identity token)

| Tenant role | Tenant permissions | Inside every project |
|---|---|---|
| (every signed-in user) | tenant.read | needs membership |
| tenant_admin | all tenant permissions | acts as project_admin |
| auditor | audit.read, tenant.read | audit.read, project.read |

Project role names in the token are ignored: project access comes only from project membership,
checked in the database on every request, so removal and downgrades take effect immediately.

## Project roles (from project membership)

| Permission | Kind | project_admin | product_owner | tech_lead | developer | reviewer | qa_lead | tester | release_manager | viewer |
|---|---|---|---|---|---|---|---|---|---|---|
| project.read | read | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| project.update | admin | ✓ |  |  |  |  |  |  |  |  |
| project.archive | admin | ✓ |  |  |  |  |  |  |  |  |
| project.member.manage | admin | ✓ |  |  |  |  |  |  |  |  |
| audit.read | read | ✓ |  |  |  |  |  |  |  |  |
| artifact.propose | propose | ✓ | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  |
| artifact.review | propose | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |
| approval.decide | approve | any scope | requirement | impact, plan |  |  | test |  | release |  |
| process.execute | execute | ✓ |  | ✓ | ✓ |  | ✓ | ✓ | ✓ |  |

## Approval scopes

| Scope | May decide |
|---|---|
| requirement | product_owner, project_admin |
| plan | project_admin, tech_lead |
| impact | project_admin, tech_lead |
| test | project_admin, qa_lead |
| release | project_admin, release_manager |

The proposer of an artifact can never decide its approval (`SELF_APPROVAL_FORBIDDEN`), whatever
their role - including project_admin and tenant_admin.

## Responses

| Situation | Response | Audit |
|---|---|---|
| Project missing, in another tenant, or caller not a member | 404 NOT_FOUND (identical) | DENIED |
| Member whose roles lack the permission or approval scope | 403 FORBIDDEN + required_permission | DENIED |
| Proposer deciding their own approval | 403 SELF_APPROVAL_FORBIDDEN | DENIED |
