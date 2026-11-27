"""QA probe for INPROOM-30 (INPROOM-210): an old code citation keeps its own commit and permalink.
Committed from QA's tests/qa_inproom30history.py (own module: the code fixtures' `env` differs)."""

import pytest

from tests import test_code_search_integration as code
from tests.sync_helpers import due
from tests.test_provenance_integration import cite, resolve

env, connected, repo = code.env, code.connected, code.repo


@pytest.mark.integration
def test_old_code_citation_keeps_its_own_commit(repo):
    g = repo
    rid = g.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    citation = cite("git", rid, "main:src/billing.py", seq=1, anchor="Billing.charge")
    original = resolve(g, citation)["results"][0]["record"]
    g.gh.repos[1].commit(
        "main", {"src/billing.py": b"class Billing:\n    def charge(self):\n        return 999\n"}
    )
    due(g.tenant)
    code.drain(g)
    old = resolve(g, citation)["results"][0]["record"]
    assert old["freshness"]["status"] == "superseded"
    assert old["revision"]["verified_ref"] == original["revision"]["verified_ref"]
    assert old["source"]["url"] == original["source"]["url"]
