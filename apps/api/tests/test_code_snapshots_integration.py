"""INPROOM-173 (story INPROOM-29 AC 1, 3, 5, 6): selected branches are indexed as file revisions keyed by
git blob sha - incrementally, resumably, with renames detected and every skip explained."""

import pytest
from sqlalchemy import text

from app.content.code import filters
from app.db import get_engine
from app.sync import worker
from tests import test_connections_integration as helpers
from tests.fakes.github import git_blob_sha
from tests.sync_helpers import due

pytestmark = pytest.mark.integration
env = helpers.env
connected = helpers.connected
APP = b"import os\n\n\ndef handler(event):\n    return os.environ.get(event)\n"
FILES = {
    "src/app.py": APP,
    "src/util.ts": b"export function add(a: number, b: number) { return a + b }\n",
    "cmd/main.go": b"package main\n\nfunc main() {}\n",
    "README.md": b"# Service\n\nHow to run.\n",
    "node_modules/x/index.js": b"module.exports = 1\n",
    "web/app.min.js": b"var a=1;\n",
    "poetry.lock": b"[[package]]\n",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n",
    "big.py": b"x = 1\n" * (filters.MAX_FILE_BYTES // 6 + 1),
    "lfs.txt": b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12\n",
    "data.txt": b"\x00\x01\x02 binary",
    "docs/link.md": ("symlink", "../README.md"),
    "libs/shared": ("submodule", "f" * 40),
}


@pytest.fixture
def repo(connected):
    e = connected
    e.gh.repos[1].commit("main", FILES, base=False)
    r = e.select("1", ["main"])
    assert r.status_code == 200, r.text
    e.rid = r.json()["id"]
    return e


def index(e, schedule=True):
    """Run every runnable job (git sync, code, derive...); the code job results."""
    out = []
    while r := worker.run_once(
        "w", transports={"github": e.gh.transport()}, schedule=schedule, tenant=e.tenant
    ):
        if r["source_kind"] == "code":
            out.append(r)
        schedule = False
    return out


def snapshot(e, commit=None):
    where = "AND commit_sha=:c" if commit else ""
    return e.q(
        f"SELECT row_to_json(s) FROM code_snapshots s WHERE tenant_id=:t {where} "
        "ORDER BY started_at DESC LIMIT 1",
        c=commit,
    )


def items(e):
    with get_engine().connect() as conn:
        return {
            r.external_id: r
            for r in conn.execute(
                text(
                    "SELECT o.external_id, o.status, r.provider_version, r.seq FROM source_objects o "
                    "JOIN source_revisions r ON r.id = o.current_revision_id "
                    "WHERE o.tenant_id=:t AND o.source_kind='git'"
                ),
                {"t": e.tenant},
            )
        }


def blob_calls(e):
    return sum(1 for _, p in e.gh.calls if "/git/blobs/" in p)


def test_the_first_index_stores_indexable_files_and_explains_every_skip(repo):
    e = repo
    [run] = index(e)
    assert run["status"] == "SUCCEEDED" and run["result"]["branches"]["main"]["status"] == "COMPLETE"
    stored = items(e)
    assert set(stored) == {"main:src/app.py", "main:src/util.ts", "main:cmd/main.go", "main:README.md"}
    assert stored["main:src/app.py"].provider_version == git_blob_sha(APP)  # revision identity = blob sha
    snap = snapshot(e)
    reasons = {s["path"]: s["reason"] for s in snap["skipped"]}
    assert reasons["node_modules/x/index.js"] == "vendored or generated directory"
    assert reasons["web/app.min.js"] == reasons["poetry.lock"] == "generated file"
    assert reasons["big.py"].startswith("larger than")
    assert reasons["lfs.txt"].startswith("Git LFS pointer") and reasons["data.txt"] == "binary content"
    assert reasons["docs/link.md"].startswith("symbolic link") and reasons["libs/shared"].startswith(
        "submodule"
    )
    assert "assets/logo.png" not in reasons  # simply not code: not listed
    assert (snap["counts"]["files"], snap["truncated"]) == (4, False)
    raw = e.client.get(
        f"/v1/projects/{e.pid}/content/git/{e.rid}/main:src/app.py/revisions/1/raw", headers=e.h()
    )
    assert raw.status_code == 200 and raw.content == APP  # item ids with "/" route fine


def test_an_unchanged_commit_downloads_nothing_and_a_rename_is_not_downloaded_again(repo):
    e = repo
    index(e)
    before = blob_calls(e)
    due(e.tenant)
    index(e)
    assert blob_calls(e) == before  # same head: snapshot already COMPLETE
    e.gh.repos[1].commit("main", {"src/app.py": None, "src/handler.py": APP, "README.md": b"# Service v2\n"})
    due(e.tenant)
    [run] = index(e)
    main = run["result"]["branches"]["main"]
    assert (main["fetched"], main["reused"]) == (1, 1)  # only README changed; the rename reused its blob
    snap = snapshot(e)
    assert snap["renames"] == [{"from": "src/app.py", "to": "src/handler.py", "blob_sha": git_blob_sha(APP)}]
    stored = items(e)
    assert stored["main:src/app.py"].status == "DELETED" and stored["main:src/handler.py"].status == "CURRENT"
    assert stored["main:README.md"].seq == 2


def test_a_truncated_tree_or_an_unreadable_blob_makes_a_partial_snapshot_that_removes_nothing(repo):
    e = repo
    index(e)
    e.gh.repos[1].commit("main", {"src/new.py": b"def n():\n    pass\n"})
    e.gh.repos[1].truncate_trees = True
    due(e.tenant)
    index(e)
    snap = snapshot(e)
    assert (snap["status"], snap["truncated"], snap["counts"]["removed"]) == ("PARTIAL", True, 0)
    assert all(i.status == "CURRENT" for i in items(e).values())  # absence from a cut tree proves nothing
    e.gh.repos[1].truncate_trees = False
    e.gh.repos[1].commit("main", {"src/secret.py": b"KEY = 1\n", "cmd/main.go": None})
    e.gh.repos[1].denied_blobs.add(git_blob_sha(b"KEY = 1\n"))
    due(e.tenant)
    index(e)
    snap = snapshot(e)
    assert snap["status"] == "PARTIAL" and snap["counts"]["inaccessible"] == 1
    assert {"path": "src/secret.py", "reason": "not accessible to the connection"} in snap["skipped"]
    assert items(e)["main:cmd/main.go"].status == "CURRENT"  # partial: nothing removed


def test_large_snapshots_resume_across_runs_without_duplicates(repo, monkeypatch):
    e = repo
    monkeypatch.setattr(filters, "BLOBS_PER_RUN", 2)
    runs = index(e)
    assert [r["result"]["more"] for r in runs] == [True, True, False]  # 4 files, 2 downloads per run
    assert snapshot(e)["status"] == "COMPLETE" and len(items(e)) == 4
    assert all(i.seq == 1 for i in items(e).values())  # nothing recorded twice
    revisions = e.q("SELECT count(*) FROM source_revisions WHERE tenant_id=:t")
    assert revisions == 4


def test_indexed_code_gets_symbols_and_a_syntax_error_fails_alone(repo):
    """INPROOM-174: symbols@1 runs on every indexed code file; one broken file does not block the others."""
    e = repo
    e.gh.repos[1].commit("main", {"src/broken.py": b"def broken(:\n    pass\n"})
    due(e.tenant)
    index(e)

    def symbols(path):
        url = f"/v1/projects/{e.pid}/content/git/{e.rid}/main:{path}/revisions/1/derivatives"
        return [d for d in e.client.get(url, headers=e.h()).json()["items"] if d["producer"] == "symbols"]

    [good] = symbols("src/app.py")
    assert (good["kind"], good["status"]) == ("code", "READY")
    [bad] = symbols("src/broken.py")
    assert bad["status"] == "FAILED" and bad["error"]["code"] == "CODE_SYNTAX_ERROR"
    assert symbols("README.md") == []  # docs are parsed (parse@2), not symbol-indexed
