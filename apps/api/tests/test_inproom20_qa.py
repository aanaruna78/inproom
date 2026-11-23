"""INPROOM-20 independent QA (author: QA): combined roles and denied membership operations.

Defects: INPROOM-99 (QA-20-01), INPROOM-100 (QA-20-02).
"""

import pytest

from tests import test_members_integration as helpers

env = helpers.env
pytestmark = pytest.mark.integration


@pytest.mark.xfail(strict=True, reason="INPROOM-99 open")
def test_auditor_with_project_admin_membership_remains_read_only(env):
    assert env.put("dave", "project_admin").status_code == 200
    response = env.client.patch(
        f"/v1/projects/{env.pid}",
        json={"name": "Auditor must not change this"},
        headers=env.h("dave", ("auditor",), **{"If-Match": '"1"'}),
    )
    assert response.status_code == 403, response.text
    assert response.json()["required_permission"] == "project.update"


@pytest.mark.xfail(strict=True, reason="INPROOM-99 open")
def test_auditor_permissions_do_not_inherit_project_membership_writes(env):
    assert env.put("dave", "project_admin").status_code == 200
    response = env.client.get(f"/v1/projects/{env.pid}/permissions/me", headers=env.h("dave", ("auditor",)))
    assert response.status_code == 200
    assert set(response.json()["permissions"]) == {"project.read", "audit.read"}


@pytest.mark.xfail(strict=True, reason="INPROOM-100 open")
@pytest.mark.parametrize("operation", ["remove_last_admin", "downgrade_last_admin", "remove_missing"])
def test_denied_membership_change_is_audited(env, operation):
    if operation == "remove_last_admin":
        response = env.delete("alice")
        expected_status, expected_code = 409, "LAST_PROJECT_ADMIN"
    elif operation == "downgrade_last_admin":
        response = env.put("alice", "viewer")
        expected_status, expected_code = 409, "LAST_PROJECT_ADMIN"
    else:
        response = env.delete("dave")
        expected_status, expected_code = 404, "MEMBER_NOT_FOUND"
    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert env.outbox() == 0
    assert (
        env.count(
            "SELECT count(*) FROM audit_events WHERE tenant_id=:t AND actor_id='alice' AND trace_id=:trace",
            trace=response.json()["trace_id"],
        )
        == 1
    )
