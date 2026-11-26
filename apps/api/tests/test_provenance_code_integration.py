"""INPROOM-182 (story INPROOM-30 AC 1): provenance of code symbols - by name or by line anchor."""

import pytest

from tests import test_code_search_integration as code
from tests.test_provenance_integration import cite, resolve

pytestmark = pytest.mark.integration
env, connected, repo = code.env, code.connected, code.repo


def test_code_symbols_by_name_and_by_line(repo):
    g = repo
    rid = g.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    head = g.gh.repos[1].branches["main"]
    by_name, by_line = (
        x["record"]
        for x in resolve(
            g,
            cite("git", rid, "main:src/billing.py", anchor="Billing.charge"),
            cite("git", rid, "main:src/billing.py", anchor="L2"),
        )["results"]
    )
    assert by_name["passage"] == by_line["passage"] | {"anchor": "Billing.charge"}
    assert (by_line["passage"]["line_start"], by_line["passage"]["line_end"]) == (2, 3)
    assert by_line["source"]["url"] == f"https://github.com/acme/api/blob/{head}/src/billing.py"
    assert by_line["revision"]["verified_ref"] == head and by_line["derivation"]["producer"] == "symbols"
