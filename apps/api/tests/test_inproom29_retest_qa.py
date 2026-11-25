"""QA re-test probes for INPROOM-29 at ea20754 (apps/api/tests/qa_inproom29_retest.py), committed as
regressions. They covered, and now guard against: INPROOM-177 (a crash between a file's new revision and
its verified commit), INPROOM-179 (retiring "feature_one" must not touch "featureXone": LIKE wildcards).
"""

import pytest

from app.content.code import snapshot
from tests import test_code_search_integration as base
from tests.sync_helpers import due
from tests.test_code_resilience_integration import run

pytestmark = pytest.mark.integration
env, connected, repo = base.env, base.connected, base.repo


def test_changed_file_citation_is_correct_even_if_the_code_job_crashes(repo, monkeypatch):
    e = repo
    head = e.gh.repos[1].commit(
        "main",
        {
            "src/billing.py": b"def refund():\n    pass\n",
            "web/handler.ts": b"export function afterRefund() {}\n",
        },
    )
    original = snapshot.record_revision

    def crash_after_billing(*args, **kwargs):
        if kwargs["external_id"] == "main:web/handler.ts":
            raise RuntimeError("worker stopped after the first changed revision committed")
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshot, "record_revision", crash_after_billing)
    due(e.tenant)
    assert run(e, kind="git", schedule=True)["status"] == "SUCCEEDED"
    crashed = run(e)
    assert crashed["status"] == "QUEUED" and crashed["error"] == "INTERNAL_ERROR", crashed
    monkeypatch.setattr(snapshot, "record_revision", original)
    assert run(e, kind="derive")["status"] == "SUCCEEDED"
    hits = base.search(e, "refund")["items"]
    assert len(hits) == 1, hits
    params = {"repository": "acme/api", "branch": "main", "path": "src/billing.py"}
    response = e.client.get(f"/v1/projects/{e.pid}/code/files", params=params, headers=e.h())
    assert response.status_code == 200, response.text
    commits = [hits[0]["citation"]["commit"], response.json()["citation"]["commit"]]
    assert commits == [head, head], {"expected_commit": head, "actual_commits": commits}


@pytest.mark.parametrize(
    "retired_branch",
    [
        "feature_one",
        "feature-one",
    ],
)
def test_retiring_a_branch_never_excludes_a_different_selected_branch(repo, retired_branch):
    e = repo
    retained = "featureXone"
    e.gh.repos[1].commit(retired_branch, {"old.py": b"def old_branch():\n    pass\n"}, base=False)
    e.gh.repos[1].commit(retained, {"keep.py": b"def keep_branch():\n    pass\n"}, base=False)
    assert e.select("1", ["main", retired_branch, retained]).status_code == 200
    due(e.tenant)
    base.drain(e)
    assert len(base.search(e, "keep_branch", branch=retained)["items"]) == 1
    assert e.select("1", ["main", retained]).status_code == 200
    due(e.tenant)
    base.drain(e)
    assert base.search(e, "old_branch")["items"] == []
    hits = base.search(e, "keep_branch", branch=retained)["items"]
    assert len(hits) == 1, {"selected_branch": retained, "retired_branch": retired_branch, "hits": hits}
