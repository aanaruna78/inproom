"""INPROOM-176 (story INPROOM-29 AC 6, 8): the code index recovers exactly from outages, crashes and rate
limits, reports deleted branches, and a paused repository is not indexed."""

import pytest

from app.content.code import filters, snapshot
from app.sync import worker
from tests import test_code_snapshots_integration as base
from tests.sync_helpers import due

pytestmark = pytest.mark.integration
env, connected, repo = base.env, base.connected, base.repo
FILES = 4  # indexable files in base.FILES


def run(e, kind="code", schedule=False):
    """Run jobs until one of `kind` has run (other follow-ups just run); return its outcome."""
    while True:
        out = worker.run_once(
            "w", transports={"github": e.gh.transport()}, schedule=schedule, tenant=e.tenant
        )
        schedule = False
        if out is None or out["source_kind"] == kind:
            return out


def git_sync(e):
    """Run the first git sync (it queues the code job)."""
    out = run(e, kind="git", schedule=True)
    assert out is not None and out["status"] == "SUCCEEDED", out
    return out


def revisions(e):
    return e.q("SELECT count(*) FROM source_revisions WHERE tenant_id=:t")


def test_an_outage_mid_snapshot_retries_and_resumes_without_duplicates(repo, monkeypatch):
    e = repo
    monkeypatch.setattr(filters, "BLOBS_PER_RUN", 2)
    git_sync(e)
    out = run(e)
    assert out["source_kind"] == "code" and out["result"]["more"] is True and revisions(e) == 2
    e.gh.path_failures = [("/git/", "500")] * 3  # the next git data request: every retry fails
    failed = run(e)
    assert (failed["source_kind"], failed["status"]) == ("code", "QUEUED")  # scheduled to retry
    due(e.tenant)
    base.index(e, schedule=False)
    assert base.snapshot(e)["status"] == "COMPLETE" and revisions(e) == FILES  # exact, nothing twice


def test_a_crash_after_some_files_restarts_exactly(repo, monkeypatch):
    e = repo
    real, calls = snapshot.record_revision, []

    def crash_on_third(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("worker killed")
        return real(*args, **kwargs)

    monkeypatch.setattr(snapshot, "record_revision", crash_on_third)
    crashed = [r for r in base.index(e) if r["status"] != "SUCCEEDED"]
    assert crashed and crashed[0]["error"] == "INTERNAL_ERROR" and revisions(e) == 2
    monkeypatch.setattr(snapshot, "record_revision", real)
    due(e.tenant)
    base.index(e, schedule=False)
    assert base.snapshot(e)["status"] == "COMPLETE" and revisions(e) == FILES
    assert all(i.seq == 1 for i in base.items(e).values())


def test_a_rate_limit_waits_without_losing_work(repo):
    e = repo
    git_sync(e)
    e.gh.path_failures = [("/git/", "ratelimit")]  # only git data requests hit the limit
    limited = run(e)
    assert (limited["source_kind"], limited["status"], limited["error"]) == (
        "code",
        "QUEUED",
        "PROVIDER_RATE_LIMITED",
    )
    assert e.q(
        "SELECT run_after > now() + interval '30 seconds' FROM sync_jobs WHERE tenant_id=:t "
        "AND source_kind='code' AND status='QUEUED'"
    )  # honours retry-after (42s)
    due(e.tenant)
    base.index(e, schedule=False)
    assert base.snapshot(e)["status"] == "COMPLETE" and revisions(e) == FILES


def test_a_deleted_branch_is_reported_and_removes_nothing(repo):
    """Deleting a selected branch pauses the repository (INPROOM-26): indexing stops, nothing is removed,
    and the snapshot list says why."""
    e = repo
    base.index(e)
    del e.gh.repos[1].branches["main"]
    due(e.tenant)
    results = base.index(e)
    assert results[-1]["status"] == "SKIPPED" and "branch not found" in results[-1]["reason"]
    assert all(i.status == "CURRENT" for i in base.items(e).values())  # nothing deleted
    snaps = e.client.get(f"/v1/projects/{e.pid}/code/snapshots", headers=e.h()).json()
    main = next(s for s in snaps if s["branch"] == "main")
    assert main["repository_status"] == "PAUSED" and main["status"] == "COMPLETE"  # last good index kept


def test_a_paused_repository_is_not_indexed(repo):
    e = repo
    rid = e.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    git_sync(e)
    assert e.client.post(f"/v1/projects/{e.pid}/sources/git/{rid}/exclude", headers=e.h()).is_success
    skipped = run(e)
    assert (skipped["source_kind"], skipped["status"]) == ("code", "SKIPPED")
    assert revisions(e) == 0
