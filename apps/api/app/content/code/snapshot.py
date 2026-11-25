"""The `code` job: index each selected branch's head commit as file revisions (INPROOM-173 / INPROOM-29).

Per branch, one code_snapshots row per commit. A run reads the commit's tree, keeps the indexable files (see
filters), and records each as a revision of item `branch:path` whose provider version is the git blob sha. It
is incremental and resumable WITHOUT a cursor: every run recomputes "files whose stored blob sha differs" and
processes at most BLOBS_PER_RUN downloads, then re-queues itself; a crash or retry just recomputes the same
set, and record_revision never duplicates a revision.

- A blob already stored for this repository (an unchanged file elsewhere, a rename, a copy) is read from the
  content store, not downloaded again.
- Only a COMPLETE snapshot (full tree, every file read) deletes paths that are gone; a truncated tree, the
  file cap, or unreadable files make it PARTIAL, which removes nothing. Renames are reported (same blob,
  old path gone, new path added).
- Drift: the snapshot's commit vs the git sync's watermark head (INPROOM-175 surfaces `behind`).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.connectors.base import NotFoundOrNoAccess, Tree
from app.connectors.http import DownloadTooLarge
from app.connectors.registry import provider_for
from app.content import ingest
from app.content import store as blobs
from app.content.code import filters
from app.content.revisions import IN_SCOPE, record_revision
from app.db import tenant_session
from app.db_schema import (
    code_snapshots,
    project_repositories,
    projects,
    source_objects,
    source_revisions,
    source_sync_state,
)
from app.sync import queue
from app.sync.worker import JobContext, SkipJob, handler

log = logging.getLogger("inproom.content.code")
AUTO_INDEX = True  # the git sync queues code jobs; source-queue test suites switch it off
MAX_SKIPPED_LISTED = 200


def schedule(conn: Connection, tenant: uuid.UUID, project_id, repo_source_id, reason: str) -> None:
    if AUTO_INDEX:
        queue.enqueue(conn, tenant, project_id, "code", repo_source_id, reason)


def item_id(branch: str, path: str) -> str:
    return f"{branch}:{path}"


def _heads(conn: Connection, repo_source_id) -> dict[str, str]:
    mark = conn.execute(
        select(source_sync_state.c.watermark).where(
            source_sync_state.c.source_kind == "git", source_sync_state.c.source_id == repo_source_id
        )
    ).scalar()
    return dict((mark or {}).get("heads") or {})


def _snapshot(conn: Connection, tenant, repo: dict[str, Any], branch: str, commit: str) -> dict[str, Any]:
    stmt = pg_insert(code_snapshots).values(
        tenant_id=tenant,
        project_id=repo["project_id"],
        source_id=repo["id"],
        branch=branch,
        commit_sha=commit,
    )
    conn.execute(stmt.on_conflict_do_nothing(index_elements=["source_id", "branch", "commit_sha"]))
    return dict(
        conn.execute(
            select(code_snapshots).where(
                code_snapshots.c.source_id == repo["id"],
                code_snapshots.c.branch == branch,
                code_snapshots.c.commit_sha == commit,
            )
        )
        .mappings()
        .one()
    )


def _retire_deselected(conn: Connection, repo: dict[str, Any]) -> list[str]:
    """Branches indexed before but no longer selected (INPROOM-179): their files are withheld (EXCLUDED) and
    their snapshots RETIRED. History is kept; reselecting re-activates them from stored blobs."""
    indexed = {
        b
        for (b,) in conn.execute(
            select(func.split_part(source_objects.c.external_id, ":", 1))
            .where(
                source_objects.c.project_id == repo["project_id"],
                source_objects.c.source_kind == "git",
                source_objects.c.source_id == repo["id"],
                source_objects.c.status == IN_SCOPE,
            )
            .distinct()
        )
    } | set(
        conn.execute(
            select(code_snapshots.c.branch)
            .where(code_snapshots.c.source_id == repo["id"], code_snapshots.c.status != "RETIRED")
            .distinct()
        ).scalars()
    )
    gone = sorted(indexed - set(repo["branches"]))
    for branch in gone:
        ids = (
            conn.execute(
                select(source_objects.c.external_id).where(
                    source_objects.c.project_id == repo["project_id"],
                    source_objects.c.source_kind == "git",
                    source_objects.c.source_id == repo["id"],
                    source_objects.c.external_id.startswith(
                        f"{branch}:", autoescape=True
                    ),  # "_"/"%" are literal
                    source_objects.c.status == IN_SCOPE,
                )
            )
            .scalars()
            .all()
        )
        ingest.mark(
            conn, repo["project_id"], "git", repo["id"], ids, "EXCLUDED", f"branch {branch} not selected"
        )
        conn.execute(
            update(code_snapshots)
            .where(code_snapshots.c.source_id == repo["id"], code_snapshots.c.branch == branch)
            .values(status="RETIRED")
        )
    return gone


def _active(conn: Connection, src, branch: str):
    """The snapshot the branch's files currently reflect: the most recently FINISHED one."""
    return conn.execute(
        select(code_snapshots.c.id)
        .where(
            code_snapshots.c.source_id == src,
            code_snapshots.c.branch == branch,
            code_snapshots.c.status.in_(("COMPLETE", "PARTIAL")),
        )
        .order_by(code_snapshots.c.finished_at.desc().nulls_last())
        .limit(1)
    ).scalar()


def _reopen(conn: Connection, snap: dict[str, Any]) -> dict[str, Any]:
    content_verdicts = {k: v for k, v in (snap["skipped_blobs"] or {}).items() if not v.get("access")}
    return dict(
        conn.execute(
            update(code_snapshots)
            .where(code_snapshots.c.id == snap["id"])
            .values(
                status="RUNNING",
                counts={},
                skipped=[],
                renames=[],
                skipped_blobs=content_verdicts,
                started_at=datetime.now(UTC),
                finished_at=None,
            )
            .returning(*code_snapshots.c)
        )
        .mappings()
        .one()
    )


def _stored_blobs(conn: Connection, project_id, repo_source_id) -> dict[str, str]:
    """git blob sha -> content-store key, for blobs this repository already holds (any branch or path)."""
    rows = conn.execute(
        select(source_revisions.c.provider_version, source_revisions.c.blob_key)
        .select_from(
            source_revisions.join(source_objects, source_objects.c.id == source_revisions.c.object_id)
        )
        .where(
            source_objects.c.project_id == project_id,
            source_objects.c.source_kind == "git",
            source_objects.c.source_id == repo_source_id,
            source_revisions.c.blob_key.is_not(None),
            source_revisions.c.purged_at.is_(None),
        )
    )
    return {r.provider_version: r.blob_key for r in rows if r.provider_version}


def _skip(run: dict[str, Any], path: str, reason: str) -> None:
    run["skipped"][reason] = run["skipped"].get(reason, 0) + 1
    if len(run["skipped_items"]) < MAX_SKIPPED_LISTED:
        run["skipped_items"].append({"path": path, "reason": reason})


def _merge(snap: dict[str, Any], run: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    counts = dict(snap["counts"] or {})
    for key in ("fetched", "reused", "stored", "unchanged", "inaccessible"):
        counts[key] = counts.get(key, 0) + run[key]
    skipped_counts = dict(counts.get("skipped") or {})
    for reason, n in run["skipped"].items():
        skipped_counts[reason] = skipped_counts.get(reason, 0) + n
    counts["skipped"] = skipped_counts
    listed = list(snap["skipped"] or [])
    for item in run["skipped_items"]:
        if item not in listed and len(listed) < MAX_SKIPPED_LISTED:
            listed.append(item)
    return counts, listed


def _verify(conn: Connection, project, src, commit: str, run: dict[str, Any]) -> None:
    """Record what this run established at `commit` (INPROOM-177): confirmed items are verified there; items
    whose content at this commit cannot be indexed are withheld (EXCLUDED), never left as stale code that
    would be cited at a commit which does not contain it. Unreadable ("not accessible") files keep their
    previous content and its older verified commit."""
    if run["confirmed"]:
        conn.execute(
            update(source_objects)
            .where(
                source_objects.c.project_id == project,
                source_objects.c.source_kind == "git",
                source_objects.c.source_id == src,
                source_objects.c.external_id.in_(run["confirmed"]),
            )
            .values(verified_ref=commit)
        )
    by_reason: dict[str, list[str]] = {}
    for external, reason in run["withdrawn"].items():
        by_reason.setdefault(reason, []).append(external)
    for reason, ids in by_reason.items():
        ingest.mark(conn, project, "git", src, ids, "EXCLUDED", f"{reason} at {commit[:12]}")


def _codeowners(
    provider, repo: dict[str, Any], tree: Tree, known: dict | None = None
) -> dict[str, Any] | None:
    paths = {e.path: e for e in tree.entries if e.type == "blob"}
    for path in filters.CODEOWNERS_PATHS:
        entry = paths.get(path)
        if entry is None:
            continue
        if known and known.get("sha") == entry.sha and known.get("text") is not None:
            return known  # already read for this commit (e.g. a re-activated snapshot): not fetched again
        try:
            data = provider.blob(repo["provider_repo_id"], entry.sha, filters.MAX_CODEOWNERS_BYTES)
            return {"path": path, "sha": entry.sha, "text": data.decode("utf-8", errors="replace")}
        except (NotFoundOrNoAccess, DownloadTooLarge) as exc:
            return {"path": path, "sha": entry.sha, "text": None, "error": str(exc)[:200]}
    return None


def _index_branch(
    ctx, provider, store, repo, branch: str, commit: str, budget: int
) -> tuple[dict, int, bool]:
    """Index one branch head. Returns (summary, remaining budget, more work left)."""
    tenant, project, src = ctx.tenant, repo["project_id"], repo["id"]
    with tenant_session(tenant) as conn:
        snap = _snapshot(conn, tenant, repo, branch, commit)
        if snap["status"] != "RUNNING":
            if _active(conn, src, branch) == snap["id"]:
                return {"commit": commit, "status": snap["status"]}, budget, False
            # The branch went back to a commit indexed before (a rollback or force-push, INPROOM-178): the
            # files now reflect another commit, so this one is indexed again - stored blobs are reused and
            # content verdicts are kept, so nothing is downloaded twice.
            snap = _reopen(conn, snap)
    try:
        tree = provider.tree(repo["provider_repo_id"], commit)
    except NotFoundOrNoAccess:
        return {"commit": commit, "status": "COMMIT_NOT_FOUND"}, budget, False
    if snap["codeowners"] is None or not snap["counts"]:
        # CODEOWNERS of THIS commit, recorded on the first run: files verified at this commit - even while
        # the snapshot is still running - get their owners from it (INPROOM-180).
        owners = _codeowners(provider, repo, tree, snap["codeowners"])
        with tenant_session(tenant) as conn:
            conn.execute(
                update(code_snapshots).where(code_snapshots.c.id == snap["id"]).values(codeowners=owners)
            )
        snap["codeowners"] = owners
    # Blobs this snapshot already turned down are not fetched again. Content verdicts (LFS, binary, size)
    # belong to the blob itself, so they carry over from the branch's previous snapshot; "not accessible"
    # is retried at the next commit.
    rejected: dict[str, dict[str, Any]] = dict(snap["skipped_blobs"] or {})
    inherited: dict[str, dict[str, Any]] = {}
    if not snap["counts"]:  # first run of this snapshot
        with tenant_session(tenant) as conn:
            previous = conn.execute(
                select(code_snapshots.c.skipped_blobs)
                .where(
                    code_snapshots.c.source_id == src,
                    code_snapshots.c.branch == branch,
                    code_snapshots.c.id != snap["id"],
                    code_snapshots.c.status != "RUNNING",
                )
                .order_by(code_snapshots.c.started_at.desc())
                .limit(1)
            ).scalar()
        inherited = {sha: v for sha, v in (previous or {}).items() if not v.get("access")}
    run: dict[str, Any] = {
        "fetched": 0,
        "reused": 0,
        "stored": 0,
        "unchanged": 0,
        "inaccessible": 0,
        "skipped": {},
        "skipped_items": [],
        "rejected": {},
        "confirmed": [],  # items whose current content is verified at this commit (INPROOM-177)
        "withdrawn": {},  # item -> reason: its content at this commit is not indexable (binary, LFS, size)
    }
    indexable = []
    for entry in sorted(tree.entries, key=lambda e: e.path):
        decision = filters.classify(entry)
        if decision.index:
            indexable.append((entry, decision))
        elif decision.reason:
            _skip(run, entry.path, decision.reason)
    capped = len(indexable) > filters.MAX_FILES
    if capped:
        for entry, _ in indexable[filters.MAX_FILES :]:
            _skip(run, entry.path, f"over the {filters.MAX_FILES}-file limit of a snapshot")
        indexable = indexable[: filters.MAX_FILES]
    prefix = f"{branch}:"
    with tenant_session(tenant) as conn:
        have = {
            k: v for k, v in ingest.current_items(conn, project, "git", src).items() if k.startswith(prefix)
        }
        stored = _stored_blobs(conn, project, src)
    # The commit rides along with each revision: a citation can always name a commit at which that exact
    # content was seen (INPROOM-175), even when the branch's latest snapshot is partial.
    acl = {"repository": repo["full_name"], "private": repo["private"], "branch": branch, "commit": commit}
    more = False
    for entry, decision in indexable:
        external = item_id(branch, entry.path)
        current = have.get(external)
        if current and current["sha"] and current["version"] == entry.sha and current["status"] == IN_SCOPE:
            run["unchanged"] += 1
            run["confirmed"].append(external)
            continue
        if entry.sha in rejected:  # turned down earlier in this snapshot (already listed): not fetched again
            if not rejected[entry.sha].get("access"):
                run["withdrawn"][external] = rejected[entry.sha]["reason"]
            continue
        if entry.sha in inherited:
            _skip(run, entry.path, inherited[entry.sha]["reason"])  # same blob as before: same verdict
            run["rejected"][entry.sha] = inherited[entry.sha]
            run["withdrawn"][external] = inherited[entry.sha]["reason"]
            continue
        data = None
        if entry.sha in stored:
            try:
                data = blobs.read(store, stored[entry.sha])
                run["reused"] += 1
            except (blobs.BlobMissing, blobs.BlobCorrupt):
                data = None
        if data is None:
            if budget <= 0:
                more = True
                break
            try:
                data = provider.blob(repo["provider_repo_id"], entry.sha, filters.MAX_FILE_BYTES)
            except DownloadTooLarge:
                reason = f"larger than {filters.MAX_FILE_BYTES} bytes"
                _skip(run, entry.path, reason)
                run["rejected"][entry.sha] = {"reason": reason, "access": False}
                run["withdrawn"][external] = reason
                continue
            except NotFoundOrNoAccess:
                run["inaccessible"] += 1
                _skip(run, entry.path, "not accessible to the connection")
                run["rejected"][entry.sha] = {"reason": "not accessible to the connection", "access": True}
                continue
            budget -= 1
            run["fetched"] += 1
        problem = filters.content_problem(data)
        if problem:
            _skip(run, entry.path, problem)
            run["rejected"][entry.sha] = {"reason": problem, "access": False}
            run["withdrawn"][external] = problem
            continue
        with tenant_session(tenant) as conn:
            done = record_revision(
                conn,
                store,
                tenant=tenant,
                project_id=project,
                source_kind="git",
                source_id=src,
                external_id=item_id(branch, entry.path),
                data=data,
                media_type=decision.media_type,
                title=entry.path,
                uri=None,  # citations use the item's verified_ref (set below), not a per-revision URI
                provider_version=entry.sha,
                acl=acl,
                job_id=ctx.job["id"],
            )
            # The revision and the commit it is verified at become visible TOGETHER: a crash later in the run
            # can never leave new content citing the previous commit (INPROOM-177).
            conn.execute(
                update(source_objects)
                .where(source_objects.c.id == done.object_id)
                .values(verified_ref=commit)
            )
        run["confirmed"].append(external)
        if done.created:
            run["stored"] += 1
            stored.setdefault(entry.sha, blobs.blob_key(tenant, done.content_sha256))
        else:
            run["unchanged"] += 1
    with tenant_session(tenant) as conn:
        snap = dict(
            conn.execute(select(code_snapshots).where(code_snapshots.c.id == snap["id"]).with_for_update())
            .mappings()
            .one()
        )
        counts, listed = _merge(snap, run)
        _verify(conn, project, src, commit, run)
        values: dict[str, Any] = {
            "counts": counts,
            "skipped": listed,
            "truncated": tree.truncated or capped,
            "skipped_blobs": dict(snap["skipped_blobs"] or {}) | run["rejected"],
        }
        if not more:
            complete = not tree.truncated and not capped and counts.get("inaccessible", 0) == 0
            in_tree = {item_id(branch, e.path) for e, _ in indexable}
            gone = [k for k, v in have.items() if k not in in_tree and v["status"] != "DELETED"]
            renames = []
            if complete:
                added = {e.sha: e.path for e, _ in indexable if item_id(branch, e.path) not in have}
                for k in gone:
                    target = added.get(have[k]["version"])
                    if target:
                        renames.append(
                            {"from": k[len(prefix) :], "to": target, "blob_sha": have[k]["version"]}
                        )
                ingest.mark(conn, project, "git", src, gone, "DELETED", f"not in {branch} at {commit[:12]}")
            values |= {
                "status": "COMPLETE" if complete else "PARTIAL",
                "renames": renames,
                "finished_at": datetime.now(UTC),
            }
            files = conn.execute(  # items of this branch indexed now
                select(source_objects.c.id).where(
                    source_objects.c.project_id == project,
                    source_objects.c.source_kind == "git",
                    source_objects.c.source_id == src,
                    source_objects.c.external_id.startswith(
                        f"{branch}:", autoescape=True
                    ),  # "_"/"%" are literal
                    source_objects.c.status == IN_SCOPE,
                )
            ).all()
            values["counts"] = counts | {"files": len(files), "removed": len(gone) if complete else 0}
        conn.execute(update(code_snapshots).where(code_snapshots.c.id == snap["id"]).values(**values))
    summary = (
        {"commit": commit, "status": values.get("status", "RUNNING")}
        | {k: run[k] for k in ("fetched", "reused", "stored", "unchanged", "inaccessible")}
        | {"skipped": run["skipped"], "renamed": len(values.get("renames") or [])}
    )
    return summary, budget, more


@handler("code")
def code(ctx: JobContext) -> dict[str, Any]:
    from app.sync.handlers import load_connection_and_token  # late: handlers -> worker -> here

    tenant = ctx.tenant
    with tenant_session(tenant) as conn:
        repo = (
            conn.execute(
                select(project_repositories).where(project_repositories.c.id == ctx.job["source_id"])
            )
            .mappings()
            .first()
        )
        if repo is None:
            raise SkipJob("repository no longer selected")
        if repo["status"] != "ACTIVE":
            raise SkipJob(f"repository is paused: {repo['status_reason']}")
        status = conn.execute(select(projects.c.status).where(projects.c.id == repo["project_id"])).scalar()
        if status == "ARCHIVED":
            raise SkipJob("project is archived")
        repo = dict(repo)
        heads = _heads(conn, repo["id"])
        row, token = load_connection_and_token(conn, tenant, repo["connection_id"])
    with tenant_session(tenant) as conn:
        retired = _retire_deselected(conn, repo)
    provider = provider_for(row["provider"], row["base_url"], token, ctx.transports)
    store = blobs.get_store()
    budget, more, branches = filters.BLOBS_PER_RUN, False, {}
    for branch in repo["branches"]:
        commit = heads.get(branch)
        if not commit:
            branches[branch] = {"status": "BRANCH_NOT_SYNCED"}
            continue
        branches[branch], budget, more = _index_branch(ctx, provider, store, repo, branch, commit, budget)
        if more:
            break
    result = {"branches": branches, "more": more, "retired_branches": retired}
    with tenant_session(tenant) as conn:
        if more:
            queue.enqueue(conn, tenant, repo["project_id"], "code", repo["id"], ctx.job["reason"])
        queue.succeed(conn, ctx.job, result)
    log.info("code index repo=%s %s", repo["full_name"], result)
    return result
