"""INPROOM-187 (story INPROOM-31 AC 4): a deselected git branch is withheld by every read path - the file,
code search and live() - and served again once it is selected."""

import pytest

from app.content import derivatives
from app.db import tenant_session
from tests import test_code_search_integration as code

pytestmark = pytest.mark.integration
env, connected, repo = code.env, code.connected, code.repo


def test_a_deselected_branch_is_withheld_everywhere(repo):
    g = repo
    rid = g.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    raw = f"/v1/projects/{g.pid}/content/git/{rid}/main:src/billing.py/revisions/1/raw"

    def served():
        with tenant_session(g.tenant) as conn:
            live = {d["external_id"] for d in derivatives.live(conn, g.pid) if d["source_kind"] == "git"}
        hits = code.search(g, "charge")["items"]
        return g.client.get(raw, headers=g.h()).status_code, bool(hits), "main:src/billing.py" in live

    assert served() == (200, True, True)
    assert g.select("1", ["release/1.0"]).status_code == 200  # main deselected
    assert served() == (409, False, False)
    assert g.select("1", ["main"]).status_code == 200
    assert served() == (200, True, True)
