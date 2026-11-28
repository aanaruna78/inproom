"""INPROOM-93 (story INPROOM-20 AC 1, 4, 6): the role matrix, approver scopes and self-approval rule as
pure unit tests. EXPECTED is written independently of policy.py, from the matrix agreed in Jira, so a policy
change that is not also agreed here fails."""

import itertools

import pytest

from app.authz import policy
from app.authz.matrix import render
from app.db_schema import PROJECT_ROLE_VALUES
from scripts.export_rbac_matrix import DOC

ADMIN, PO, TL, DEV, REV, QL, TS, RM, VW = policy.PROJECT_ROLES

EXPECTED: dict[str, set[str]] = {
    "project.read": {ADMIN, PO, TL, DEV, REV, QL, TS, RM, VW},
    "project.update": {ADMIN},
    "project.archive": {ADMIN},
    "project.member.manage": {ADMIN},
    "project.settings.update": {ADMIN},
    "project.source.manage": {ADMIN},
    "project.content.upload": {ADMIN, PO, TL, QL},  # INPROOM-28 plan (agreed in Jira)
    "project.graph.curate": {ADMIN, PO, TL, QL},  # INPROOM-34 plan (agreed in Jira)
    "project.memory.publish": {ADMIN, PO, TL, QL},  # INPROOM-35 plan (agreed in Jira)
    "audit.read": {ADMIN},
    "artifact.propose": {ADMIN, PO, TL, DEV, QL, TS},
    "artifact.review": {ADMIN, PO, TL, DEV, REV, QL, TS, RM},
    "approval.decide": {ADMIN, PO, TL, QL, RM},
    "process.execute": {ADMIN, TL, DEV, QL, TS, RM},
}
EXPECTED_SCOPES: dict[str, set[str]] = {
    ADMIN: {"requirement", "plan", "impact", "test", "release"},
    PO: {"requirement"},
    TL: {"plan", "impact"},
    QL: {"test"},
    RM: {"release"},
}
NONE = frozenset()


def test_expected_matrix_covers_every_permission_and_role():
    assert set(EXPECTED) == set(policy.PROJECT_PERMISSIONS)
    assert len(policy.PROJECT_ROLES) == 9


@pytest.mark.parametrize(
    ("role", "permission"), list(itertools.product(policy.PROJECT_ROLES, policy.PROJECT_PERMISSIONS))
)
def test_role_x_permission(role, permission):
    scope = None
    if permission == "approval.decide":
        # Use a scope the role may decide when it has one, otherwise any scope.
        scope = sorted(EXPECTED_SCOPES.get(role, {"requirement"}))[0]
    decision = policy.decide(NONE, frozenset({role}), permission, scope=scope)
    assert decision.allowed is (role in EXPECTED[permission]), (role, permission)
    if not decision.allowed:
        assert decision.code == policy.FORBIDDEN


@pytest.mark.parametrize(
    ("role", "scope"), list(itertools.product(policy.PROJECT_ROLES, policy.APPROVER_SCOPES))
)
def test_approval_scope_matrix(role, scope):
    decision = policy.decide(NONE, frozenset({role}), "approval.decide", scope=scope)
    assert decision.allowed is (scope in EXPECTED_SCOPES.get(role, set())), (role, scope)


@pytest.mark.parametrize("role", [ADMIN, PO, TL, QL, RM])
def test_proposer_can_never_approve_own_proposal(role):
    scope = sorted(EXPECTED_SCOPES[role])[0]
    decision = policy.decide(NONE, frozenset({role}), "approval.decide", scope=scope, is_proposer=True)
    assert not decision.allowed and decision.code == policy.SELF_APPROVAL_FORBIDDEN


def test_tenant_admin_cannot_self_approve_either():
    tenant_admin = frozenset({policy.TENANT_ADMIN})
    assert policy.decide(tenant_admin, NONE, "approval.decide", scope="release").allowed
    denied = policy.decide(tenant_admin, NONE, "approval.decide", scope="release", is_proposer=True)
    assert denied.code == policy.SELF_APPROVAL_FORBIDDEN


def test_out_of_scope_is_forbidden_not_self_approval():
    # A tech lead proposing a release approval is denied for scope first.
    decision = policy.decide(NONE, frozenset({TL}), "approval.decide", scope="release", is_proposer=True)
    assert decision.code == policy.FORBIDDEN


def test_multiple_roles_union():
    roles = frozenset({VW, QL})
    assert policy.decide(NONE, roles, "process.execute").allowed
    assert policy.approval_scopes(NONE, roles) == {"test"}
    assert not policy.decide(NONE, roles, "project.update").allowed


def test_token_project_roles_are_ignored():
    token = frozenset(policy.PROJECT_ROLES)  # every project role, forged or stale, in the token
    assert policy.tenant_roles(token) == NONE
    assert not policy.can_see_project(token, NONE)
    assert policy.project_permissions(token, NONE) == NONE
    assert policy.tenant_permissions(token) == {"tenant.read"}


def test_tenant_admin_acts_as_project_admin_everywhere():
    t = frozenset({policy.TENANT_ADMIN})
    assert policy.can_see_project(t, NONE)
    assert policy.effective_project_roles(t, NONE) == {ADMIN}
    assert policy.project_permissions(t, NONE) == set(policy.PROJECT_PERMISSIONS)
    assert policy.has_tenant_permission(t, "tenant.workspace.create")
    assert policy.has_tenant_permission(t, "workspace.project.create")


def test_auditor_is_read_only_everywhere():
    a = frozenset({policy.AUDITOR})
    assert policy.can_see_project(a, NONE)
    assert policy.project_permissions(a, NONE) == {"project.read", "audit.read"}
    assert policy.approval_scopes(a, NONE) == NONE
    assert not policy.has_tenant_permission(a, "tenant.workspace.create")
    assert policy.has_tenant_permission(a, "audit.read")


def test_plain_member_has_only_tenant_read():
    assert policy.tenant_permissions(NONE) == {"tenant.read"}
    assert not policy.has_tenant_permission(NONE, "workspace.project.create")
    assert not policy.sees_every_project(NONE)


def test_unknown_member_role_grants_nothing():
    assert not policy.can_see_project(NONE, frozenset({"superuser"}))


def test_programming_errors_are_loud():
    with pytest.raises(ValueError):
        policy.decide(NONE, frozenset({ADMIN}), "project.delete_everything")
    with pytest.raises(ValueError):
        policy.decide(NONE, frozenset({ADMIN}), "approval.decide", scope=None)
    with pytest.raises(ValueError):
        policy.decide(NONE, frozenset({ADMIN}), "approval.decide", scope="production")


def test_db_role_constraint_matches_policy():
    assert PROJECT_ROLE_VALUES == policy.PROJECT_ROLES


def test_published_matrix_doc_is_current():
    assert DOC.exists() and DOC.read_text() == render(), "run `make rbac-matrix` and commit the result"


# ---- INPROOM-99 (QA-20-01): auditor is a read-only cap, tenant_admin overrides it


@pytest.mark.parametrize("role", policy.PROJECT_ROLES)
def test_auditor_stays_read_only_whatever_project_role_they_hold(role):
    auditor = frozenset({policy.AUDITOR})
    assert policy.project_permissions(auditor, frozenset({role})) == {"project.read", "audit.read"}
    assert policy.approval_scopes(auditor, frozenset({role})) == NONE
    for permission in (
        "project.update",
        "project.archive",
        "project.member.manage",
        "artifact.propose",
        "project.content.upload",
        "project.graph.curate",
        "project.memory.publish",
        "process.execute",
    ):
        assert policy.decide(auditor, frozenset({role}), permission).code == policy.FORBIDDEN
    for scope in policy.APPROVER_SCOPES:
        assert not policy.decide(auditor, frozenset({role}), "approval.decide", scope=scope).allowed


def test_tenant_admin_overrides_the_auditor_cap():
    both = frozenset({policy.TENANT_ADMIN, policy.AUDITOR})
    assert policy.project_permissions(both, NONE) == set(policy.PROJECT_PERMISSIONS)
    assert policy.decide(both, NONE, "project.update").allowed
