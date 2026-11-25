"""INPROOM-175 (story INPROOM-29 AC 1, 4, 7): code search hits are commit-pinned citations with owners;
drift is visible; a partial snapshot never claims a file was verified at a commit it was not."""

import pytest

from app.sync import worker
from tests import test_connections_integration as helpers
from tests.fakes.github import git_blob_sha
from tests.sync_helpers import due

pytestmark = pytest.mark.integration
env = helpers.env
connected = helpers.connected
APP = (
    b"class Billing:\n    def charge(self, amount):\n        return amount\n\n\n"
    b"def handler(event):\n    return event\n"
)
FILES = {
    "src/billing.py": APP,
    "web/handler.ts": b"export function handlerTs(req: Request) {\n  return req\n}\n",
    "cmd/main.go": b"package main\n\nfunc HandleRequest() {}\n",
    ".github/CODEOWNERS": b"* @acme/everyone\n/src/ @acme/payments\n*.go @gopher\n",
}


@pytest.fixture
def repo(connected):
    e = connected
    e.gh.repos[1].commit("main", FILES, base=False)
    assert e.select("1", ["main"]).status_code == 200
    drain(e)
    return e


def drain(e, schedule=True):
    while worker.run_once("w", transports={"github": e.gh.transport()}, schedule=schedule, tenant=e.tenant):
        schedule = False


def search(e, query, **params):
    r = e.client.get(f"/v1/projects/{e.pid}/code/search", params={"q": query} | params, headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def test_hits_are_commit_pinned_citations_with_owners(repo):
    e = repo
    head = e.gh.repos[1].branches["main"]
    [hit] = search(e, "charge")["items"]
    assert hit["symbol"] == {
        "name": "charge",
        "kind": "method",
        "container": "Billing",
        "signature": "def charge(self, amount)",
    }
    c = hit["citation"]
    assert (c["repository"], c["branch"], c["commit"], c["path"]) == (
        "acme/api",
        "main",
        head,
        "src/billing.py",
    )
    assert (c["line_start"], c["line_end"], c["blob_sha"]) == (2, 3, git_blob_sha(APP))
    assert c["permalink"] == f"https://github.com/acme/api/blob/{head}/src/billing.py#L2-L3"
    assert hit["owners"] == ["@acme/payments"] and hit["behind"] is False and hit["language"] == "python"
    assert search(e, "HandleRequest")["items"][0]["owners"] == ["@gopher"]  # last matching rule wins


def test_ranking_filters_and_paging(repo):
    e = repo
    names = [h["symbol"]["name"] for h in search(e, "handler")["items"]]
    # exact first, then prefixes alphabetically (case-insensitive: "handlerequest" < "handlerts")
    assert names == ["handler", "HandleRequest", "handlerTs"]
    assert [h["symbol"]["name"] for h in search(e, "charge")["items"]] == ["charge"]
    assert [h["symbol"]["name"] for h in search(e, "arg")["items"]] == ["charge"]  # contains
    assert [h["symbol"]["name"] for h in search(e, "handle", language="go")["items"]] == ["HandleRequest"]
    assert [h["symbol"]["name"] for h in search(e, "e", kind="class")["items"]] == []
    assert [h["symbol"]["name"] for h in search(e, "bill", kind="class")["items"]] == ["Billing"]
    first = search(e, "a", limit=2)
    rest = search(e, "a", limit=2, cursor=first["next_cursor"])
    assert first["next_cursor"] and not set(map(str, first["items"])) & set(map(str, rest["items"]))
    assert search(e, "%")["items"] == [] and search(e, "_")["items"] == []  # LIKE wildcards are literal
    bad = e.client.get(
        f"/v1/projects/{e.pid}/code/search", params={"q": "a", "cursor": "bm9wZQ"}, headers=e.h()
    )
    assert bad.json()["code"] == "INVALID_CURSOR"


def test_drift_is_visible_until_the_new_commit_is_indexed(repo, monkeypatch):
    e = repo
    old = e.gh.repos[1].branches["main"]
    new = e.gh.repos[1].commit("main", {"src/billing.py": b"def refund(amount):\n    return -amount\n"})
    from app.content.code import snapshot

    monkeypatch.setattr(snapshot, "AUTO_INDEX", False)  # the git sync sees the new head; indexing waits
    due(e.tenant)
    drain(e)
    [hit] = search(e, "charge")["items"]
    assert hit["behind"] is True and hit["citation"]["commit"] == old  # still pinned to what was indexed
    snaps = e.client.get(f"/v1/projects/{e.pid}/code/snapshots", headers=e.h()).json()
    main = next(s for s in snaps if s["repository"] == "acme/api" and s["branch"] == "main")
    assert (main["head_commit"], main["indexed_commit"], main["behind"]) == (new, old, True)
    monkeypatch.setattr(snapshot, "AUTO_INDEX", True)
    due(e.tenant)
    drain(e)
    assert search(e, "charge")["items"] == []  # removed symbols are gone
    [hit] = search(e, "refund")["items"]
    assert hit["behind"] is False and hit["citation"]["commit"] == new


def test_a_partial_snapshot_cites_the_commit_where_each_file_was_verified(repo):
    """INPROOM-177: each hit cites the newest commit at which ITS content was confirmed - a file the
    truncated tree still lists is verified at the new commit; one it omits keeps its earlier commit."""
    e = repo
    first = e.gh.repos[1].branches["main"]
    second = e.gh.repos[1].commit("main", {"zz/extra.py": b"def extra():\n    pass\n"})
    e.gh.repos[1].truncate_trees = True  # the fake keeps the first half of the sorted entries
    due(e.tenant)
    drain(e)
    snaps = e.client.get(f"/v1/projects/{e.pid}/code/snapshots", headers=e.h()).json()
    assert next(s for s in snaps if s["branch"] == "main")["status"] == "PARTIAL"
    from app.connectors.github import GitHubProvider
    from app.connectors.vault import Secret
    from tests.fakes.github import VALID_TOKEN

    gh = GitHubProvider("https://api.github.com", Secret(VALID_TOKEN), transport=e.gh.transport())
    listed = {entry.path for entry in gh.tree("1", second).entries}  # what the truncated tree showed
    [charge] = search(e, "charge")["items"]
    assert charge["citation"]["commit"] == (second if "src/billing.py" in listed else first)
    assert "web/handler.ts" not in listed  # sorts late: cut from the truncated tree
    [ts] = search(e, "handlerTs")["items"]
    assert ts["citation"]["commit"] == first and ts["behind"] is True


def test_file_symbols_and_withholding(repo):
    e = repo
    params = {"repository": "acme/api", "branch": "main", "path": "src/billing.py"}
    r = e.client.get(f"/v1/projects/{e.pid}/code/files", params=params, headers=e.h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["name"] for d in body["definitions"]] == ["Billing", "charge", "handler"]
    assert body["owners"] == ["@acme/payments"] and body["citation"]["path"] == "src/billing.py"
    missing = e.client.get(
        f"/v1/projects/{e.pid}/code/files", params=params | {"path": "nope.py"}, headers=e.h()
    )
    assert missing.status_code == 404 and missing.json()["code"] == "CODE_NOT_FOUND"
    rid = e.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    assert e.client.post(f"/v1/projects/{e.pid}/sources/git/{rid}/exclude", headers=e.h()).is_success
    assert search(e, "charge")["items"] == []  # a paused/excluded repository is withheld from search
    withheld = e.client.get(f"/v1/projects/{e.pid}/code/files", params=params, headers=e.h())
    assert withheld.status_code == 409 and withheld.json()["code"] == "CONTENT_WITHHELD"


def test_content_that_cannot_be_indexed_at_a_commit_withholds_the_old_code(repo):
    """INPROOM-177: a file replaced by binary content is not cited at the new commit; it is withheld until
    indexable content returns, and every returned hit is verified at the commit it names."""
    e = repo
    e.gh.repos[1].commit("main", {"src/billing.py": b"\x00\x01binary build artefact"})
    due(e.tenant)
    drain(e)
    assert search(e, "charge")["items"] == []
    rid = e.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    item = e.client.get(f"/v1/projects/{e.pid}/content/git/{rid}/main:src/billing.py", headers=e.h()).json()
    assert item["status"] == "EXCLUDED" and item["status_reason"].startswith("binary content at ")
    back = e.gh.repos[1].commit("main", {"src/billing.py": APP, "NOTES.txt": b"restored"})  # a new commit
    due(e.tenant)
    drain(e)
    [hit] = search(e, "charge")["items"]
    assert hit["citation"]["commit"] == back and hit["behind"] is False


def test_a_rollback_and_back_again_restores_each_index_without_downloads(repo):
    """INPROOM-178: A -> B -> A -> B: the index always reflects the head; stored blobs are never fetched
    again and no symbol is duplicated."""
    e = repo
    a = e.gh.repos[1].branches["main"]
    b = e.gh.repos[1].commit("main", {"src/billing.py": b"def refund():\n    pass\n"})
    due(e.tenant)
    drain(e)
    fetched = sum(1 for _, p in e.gh.calls if "/git/blobs/" in p)
    for head, present, absent in ((a, "charge", "refund"), (b, "refund", "charge")):
        e.gh.repos[1].branches["main"] = head
        due(e.tenant)
        drain(e)
        [hit] = search(e, present)["items"]
        assert (hit["citation"]["commit"], hit["behind"]) == (head, False)
        assert search(e, absent)["items"] == []
        snaps = e.client.get(f"/v1/projects/{e.pid}/code/snapshots", headers=e.h()).json()
        main = next(s for s in snaps if s["branch"] == "main")
        assert (main["indexed_commit"], main["behind"]) == (head, False)
    assert sum(1 for _, p in e.gh.calls if "/git/blobs/" in p) == fetched  # all from the content store
    assert (
        e.q(
            "SELECT count(*) FROM code_symbols s JOIN derivatives d ON d.id = s.derivative_id "
            "WHERE s.tenant_id=:t AND s.name='refund' AND d.status='READY'"
        )
        == 1
    )
