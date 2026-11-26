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


def test_code_search_withholds_repositories_above_the_cap(repo):
    g = repo
    rid = g.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    search = f"/v1/projects/{g.pid}/code/search"
    assert g.client.get(search, params={"q": "charge"}, headers=g.h()).json()["items"]
    r = g.client.put(
        f"/v1/projects/{g.pid}/sources/git/{rid}/policy",
        json={"tier": "trusted", "classification": "confidential", "reason": "restricted code"},
        headers=g.h(**{"If-Match": '"0"'}),
    )
    assert r.status_code == 200, r.text
    assert g.client.get(search, params={"q": "charge"}, headers=g.h()).json()["items"] == []
    params = {"repository": "acme/api", "branch": "main", "path": "src/billing.py"}
    files = g.client.get(f"/v1/projects/{g.pid}/code/files", params=params, headers=g.h())
    assert files.status_code == 409 and files.json()["reason"] == "CLASSIFICATION_EXCEEDS_PROJECT"


def test_code_hits_carry_provenance_and_say_when_the_branch_moved(repo, monkeypatch):
    from app.content.code import snapshot
    from tests.sync_helpers import due

    g = repo
    [hit] = g.client.get(f"/v1/projects/{g.pid}/code/search", params={"q": "charge"}, headers=g.h()).json()[
        "items"
    ]
    assert (hit["provenance"]["freshness"], hit["provenance"]["derivation"]) == ("current", "extracted")
    assert hit["provenance"]["retrieved_at"] and hit["provenance"]["trust"] == "trusted"
    g.gh.repos[1].commit("main", {"NOTES.txt": b"later"})
    monkeypatch.setattr(snapshot, "AUTO_INDEX", False)  # the head moves; indexing waits
    due(g.tenant)
    code.drain(g)
    [hit] = g.client.get(f"/v1/projects/{g.pid}/code/search", params={"q": "charge"}, headers=g.h()).json()[
        "items"
    ]
    assert (hit["provenance"]["freshness"], hit["provenance"]["reasons"]) == ("stale", ["CODE_BRANCH_BEHIND"])
    rid = g.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    [r] = resolve(g, cite("git", rid, "main:src/billing.py", anchor="L2"))["results"]
    assert [x["code"] for x in r["record"]["freshness"]["reasons"]] == ["CODE_BRANCH_BEHIND"]
