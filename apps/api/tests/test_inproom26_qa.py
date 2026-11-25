"""QA probes for INPROOM-26 (build/qa/INPROOM-26/test_probes.py at d6e1ce3), committed as regressions.

Unfixed cases are strict xfails naming their defect, so each fix must flip them to passing:
- INPROOM-149: a health check in flight must not undo an exclusion committed meanwhile (fixed);
- INPROOM-150: adoption must leave archived projects' sources untouched.
"""

import pytest

from app.connectors.confluence import ConfluenceProvider
from tests import test_confluence_sources_integration as helpers
from tests.fakes import confluence as fc

env = helpers.env
selected = helpers.selected
pytestmark = pytest.mark.integration


def pause_access(e):
    e.conf.revoke_space(fc.VALID_TOKEN, "ENG")
    assert e.client.post(f"/v1/connections/{e.cid}/check", headers=e.h()).status_code == 200
    assert e.q("SELECT pause_cause FROM project_confluence_sources WHERE tenant_id=:t") == "access"
    e.conf.users[fc.VALID_TOKEN].spaces["ENG"] = {"read"}


def test_health_in_flight_must_preserve_new_exclusion(selected, monkeypatch):
    e = selected
    pause_access(e)
    original = ConfluenceProvider.get_space
    fired = False

    def during_provider_call(provider, key):
        nonlocal fired
        result = original(provider, key)
        if not fired:
            fired = True
            response = e.client.post(
                f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/exclude", headers=e.h()
            )
            assert response.status_code == 200
        return result

    monkeypatch.setattr(ConfluenceProvider, "get_space", during_provider_call)
    response = e.client.post(f"/v1/connections/{e.cid}/check", headers=e.h())
    assert response.status_code == 200
    assert fired
    cause = e.q("SELECT pause_cause FROM project_confluence_sources WHERE tenant_id=:t")
    assert cause == "excluded", f"health overwrote the person's exclusion: pause_cause={cause!r}"


@pytest.mark.xfail(strict=True, reason="INPROOM-150: adoption mutates archived projects")
def test_adoption_must_preserve_archived_project_read_only(selected):
    e = selected
    assert e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h()).status_code == 200
    replacement = e.connect(name="Replacement").json()["id"]
    assert e.client.post(f"/v1/projects/{e.pid}/archive", headers=e.h()).status_code == 200
    response = e.client.post(
        f"/v1/connections/{replacement}/adopt", json={"from_connection_id": e.cid}, headers=e.h()
    )
    unchanged = str(e.q("SELECT connection_id FROM project_confluence_sources WHERE tenant_id=:t"))
    assert unchanged == e.cid, f"archived source moved to {unchanged}; HTTP {response.status_code}"


def test_repeated_reauthorization_does_not_duplicate_reconcile(selected):
    e = selected
    pause_access(e)
    url = f"/v1/connections/{e.cid}/credential"
    first = e.client.put(url, json={"token": fc.VALID_TOKEN}, headers=e.h())
    again = e.client.put(url, json={"token": fc.VALID_TOKEN}, headers=e.h())
    assert first.status_code == again.status_code == 200
    assert len(first.json()["resumed"]) == 1 and again.json()["resumed"] == []
    assert e.q("SELECT count(*) FROM sync_jobs WHERE tenant_id=:t AND hints->>'after'='reauthorization'") == 1


def test_excluded_source_survives_revoke_and_adopt(selected):
    e = selected
    assert (
        e.client.post(f"/v1/projects/{e.pid}/sources/confluence/{e.sid}/exclude", headers=e.h()).status_code
        == 200
    )
    assert e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h()).status_code == 200
    replacement = e.connect(name="Replacement").json()["id"]
    response = e.client.post(
        f"/v1/connections/{replacement}/adopt", json={"from_connection_id": e.cid}, headers=e.h()
    )
    assert response.status_code == 200
    assert len(response.json()["kept_excluded"]) == 1 and response.json()["adopted"] == []
    assert e.q("SELECT pause_cause FROM project_confluence_sources WHERE tenant_id=:t") == "excluded"


def test_adoption_rejects_other_site(selected):
    e = selected
    assert e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h()).status_code == 200
    replacement = e.connect(name="Other site", site="https://other.atlassian.net").json()["id"]
    response = e.client.post(
        f"/v1/connections/{replacement}/adopt", json={"from_connection_id": e.cid}, headers=e.h()
    )
    assert response.status_code == 409 and response.json()["code"] == "ADOPT_NOT_ALLOWED"
