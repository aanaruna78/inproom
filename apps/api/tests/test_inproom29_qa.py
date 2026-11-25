"""QA acceptance probes for INPROOM-29 at 55fa98a (apps/api/tests/qa_inproom29.py), committed as regressions.

Unfixed cases are strict xfails naming their defect: INPROOM-177 (citation commit), INPROOM-178 (rollback),
INPROOM-179 (deselected branches), INPROOM-180 (owners at the cited commit), INPROOM-181 (permalink paths).
"""

from urllib.parse import quote

import pytest

from app.content.code import filters
from tests import test_code_search_integration as base
from tests.sync_helpers import due
from tests.test_code_resilience_integration import run

pytestmark = pytest.mark.integration
env, connected, repo = base.env, base.connected, base.repo


def snapshots(e):
    response = e.client.get(f"/v1/projects/{e.pid}/code/snapshots", headers=e.h())
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "replacement",
    [b"\x00binary", b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n"],
    ids=["binary", "lfs"],
)
def test_skipped_replacement_never_cites_old_code_at_the_new_commit(repo, replacement):
    e = repo
    old = e.gh.repos[1].branches["main"]
    e.gh.repos[1].commit("main", {"src/billing.py": replacement})
    due(e.tenant)
    base.drain(e)
    # Withholding the old hit is acceptable. Retaining it must cite its verified commit.
    for hit in base.search(e, "charge")["items"]:
        assert hit["citation"]["commit"] == old, hit


def test_new_symbols_during_a_batch_never_cite_the_previous_snapshot(repo, monkeypatch):
    e = repo
    monkeypatch.setattr(filters, "BLOBS_PER_RUN", 1)
    new = e.gh.repos[1].commit(
        "main", {"aaa.py": b"def brand_new():\n    pass\n", "zzz.py": b"def later():\n    pass\n"}
    )
    due(e.tenant)
    assert run(e, kind="git", schedule=True)["status"] == "SUCCEEDED"
    assert run(e)["result"]["more"] is True
    assert run(e, kind="derive")["status"] == "SUCCEEDED"
    [hit] = base.search(e, "brand_new")["items"]
    assert hit["citation"]["commit"] == new, hit


def test_returning_to_an_already_indexed_commit_restores_that_index(repo):
    e = repo
    old = e.gh.repos[1].branches["main"]
    e.gh.repos[1].commit("main", {"src/billing.py": b"def refund():\n    pass\n"})
    due(e.tenant)
    base.drain(e)
    assert base.search(e, "refund")["items"]
    e.gh.repos[1].branches["main"] = old
    due(e.tenant)
    base.drain(e)
    hits = base.search(e, "charge")["items"]
    assert len(hits) == 1, {"hits": hits, "snapshots": snapshots(e)}
    [hit] = hits
    assert hit["citation"]["commit"] == old and hit["behind"] is False, hit
    assert base.search(e, "refund")["items"] == []


@pytest.mark.xfail(strict=True, reason="INPROOM-180: owners from another commit")
def test_partial_snapshot_owners_match_the_cited_commit(repo):
    e = repo
    old = e.gh.repos[1].branches["main"]
    e.gh.repos[1].commit("main", {".github/CODEOWNERS": b"* @acme/new-team\n"})
    e.gh.repos[1].truncate_trees = True
    due(e.tenant)
    base.drain(e)
    [hit] = base.search(e, "charge")["items"]
    assert hit["citation"]["commit"] == old, hit
    assert hit["owners"] == ["@acme/payments"], hit


def test_deselected_branch_is_no_longer_searchable(repo):
    e = repo
    e.gh.repos[1].commit("feature", {"feature.py": b"def feature_only():\n    pass\n"}, base=False)
    assert e.select("1", ["main", "feature"]).status_code == 200
    due(e.tenant)
    base.drain(e)
    assert base.search(e, "feature_only", branch="feature")["items"]
    assert e.select("1", ["main"]).status_code == 200
    due(e.tenant)
    base.drain(e)
    hits = base.search(e, "feature_only", branch="feature")["items"]
    file = e.client.get(
        f"/v1/projects/{e.pid}/code/files",
        params={"repository": "acme/api", "branch": "feature", "path": "feature.py"},
        headers=e.h(),
    )
    assert not hits and file.status_code in (404, 409), {"hits": hits, "file_status": file.status_code}


@pytest.mark.xfail(strict=True, reason="INPROOM-181: permalink path not encoded")
def test_citation_permalink_encodes_the_actual_git_path(repo):
    e = repo
    path = "src/a#b.py"
    head = e.gh.repos[1].commit("main", {path: b"def unusual_path():\n    pass\n"})
    due(e.tenant)
    base.drain(e)
    [hit] = base.search(e, "unusual_path")["items"]
    assert hit["citation"]["permalink"] == (
        f"https://github.com/acme/api/blob/{head}/{quote(path)}#L1-L2"
    ), hit
