# Knowledge refresh, deletion and ACL propagation (INPROOM-31)

When something changes at a source, INPROOM must stop serving whatever is no longer allowed. That covers:
- an item that is deleted, restricted or excluded;
- a Jira issue that gains a security level;
- a source that is paused or removed;
- a branch that is deselected;
- a policy or setting that changes.

This runbook covers the **invalidation contract**, the **knowledge change feed** that consumers follow, and how to force and check propagation.

## The invalidation contract

1. **Atomic withholding.** The commit that records a state change also withholds the affected content from every read path:
   - raw content, documents, code files;
   - code search, provenance and conflicts;
   - `derivatives.live()`.

   There is never a window in which removed content is still served. Every read path uses one visibility rule (`app/content/visibility.py`, which is the SQL form of `access_reason`).
2. **One feed entry per change, in the same commit.** The project's feed (`knowledge_changes`) gets an ordered, gap-free entry, and the transaction emits one outbox event `knowledge.changed` `{project_id, from_seq}`.
   - Database triggers write the entries, so no code path can skip them.
   - A retried or repeated sync that changes nothing writes nothing.
   - A failed sync rolls back its state changes and its entries together.
3. **Detection bounds.** INPROOM can only withhold what it knows about:

| Change | Detected |
|---|---|
| Any provider change with a webhook (GitHub, Jira, Confluence) | Seconds: the webhook queues a sync |
| Confluence page deleted, restricted, moved or relabelled | On the next sync. Every sync lists the whole space, including restrictions that don't bump the page version |
| Jira issue edited, or its security level changed | On the next incremental sync (the issue's `updated` changes) |
| Jira issue deleted, or moved out of the project | On the deletion webhook, or the daily full reconcile |
| Git file deleted or renamed | On the next COMPLETE snapshot of the branch |
| Source paused or removed, branch deselected, policy or setting changed, upload deleted | Immediately (it is the same API transaction) |

To shorten the window, register webhooks (see [sync-worker.md](sync-worker.md)), or force a refresh (see below).

## The knowledge change feed

`GET /v1/projects/{id}/knowledge/changes?after=<seq>&limit=<1..500>` (project.read) returns the following:

| Field | Meaning |
|---|---|
| `seq` | Per-project, ordered, gap-free. Store the last one you processed and pass it as `after`. |
| `change` | `invalidated`: drop it. `restored`: servable again, so rebuild. `refreshed`: new content (`revision_seq`). |
| `scope` | `item` (`kind`, `source_id`, `external_id`), or `source` (the whole source, or with `branch`, one git branch). |
| `reason` | `DELETED`, `EXCLUDED`, `RESTRICTED`, `TOO_LARGE`, `PURGED`, `NEW_REVISION`, `SOURCE_PAUSED`, `SOURCE_REMOVED`, `BRANCH_DESELECTED`, `BRANCH_SELECTED`, `UPLOADS_DISABLED`, `CLASSIFICATION_EXCEEDS_PROJECT` |

The response also gives `head` (the latest seq) and `next` (the cursor to continue from, or null when you are caught up). Entries carry **identifiers only**, never titles, URLs or content, so an invalidation can be published even after access to the item is gone.

**How a consumer (a retrieval index or a cache) should use it:**
1. On `knowledge.changed`, or on a timer, read `changes?after=<last seq>` until `next` is null.
2. Apply each entry in order:
   - an `invalidated` item: delete it;
   - an `invalidated` source or branch: delete everything from it;
   - `restored` and `refreshed`: rebuild from `derivatives.live()` or the content APIs, which already apply the visibility rule.
3. Store the last applied `seq`. A gap in the seqs means you missed something, so re-read from your stored seq.

## Jira security levels

Issues under a Jira security level are **withheld by default** (RESTRICTED, "security level X"). This mirrors Confluence `include_restricted=false`.
- A new secured issue is never stored.
- `PUT /v1/projects/{id}/sources/jira/{source_id}/security` sets the policy. It needs `If-Match` (the source version) and a `reason`. The body is `{"include_secured": true, "allowed_levels": ["Staff"]}`; an empty list allows every level, and names match case-insensitively.
- The change is applied **at once** from the stored ACL, with no refetch. Loosening the policy also queues a full sync to fetch issues that were never stored.
- Deploying migration 0021 withholds every stored issue that already has a level.

## Forcing and checking propagation

| Route | Use |
|---|---|
| `POST /v1/projects/{id}/knowledge/refresh` `{"scope": "project" \| "source", "kind"?, "source_id"?, "reason"}` | project.source.manage. Queues a **full** reconcile of every active source (or one), coalesced with jobs already queued, and is audited as `knowledge.refresh`. Paused sources are listed under `skipped`. |
| `GET /v1/projects/{id}/knowledge/status` | project.read. Gives the feed `head`, and for each source: the last change (seq and time), `deletions_verified_at` (the last reconcile that saw the source's whole item set), and `pending_derivatives`. |

## Troubleshooting

| Symptom | Cause and action |
|---|---|
| A page deleted in Confluence is still served | Check its sync: `deletions_verified_at` is older than the deletion. Look at the sync status for a failing or paused source, or refresh. |
| A deleted Jira issue is still served | Without the deletion webhook, deletions wait for the daily full reconcile. Register the Jira webhook, or refresh the source. |
| A consumer still serves removed content | It has fallen behind the feed. Compare its stored seq with `status.head`, and re-read `changes?after=`. |
| `503 OBJECTSTORE_UNAVAILABLE` on sync now or an upload | The content store is down. Nothing was changed (state and feed rolled back), so retry after `Retry-After`. |
| Secured Jira issues are missing | That is the default. Set the source's security policy if the project should serve them. |

Related: [content-store.md](content-store.md), [provenance.md](provenance.md), [code-index.md](code-index.md), [sync-worker.md](sync-worker.md).

Semantic search applies the same visibility rule, and vectors are deleted with their content: see [vector-index.md](vector-index.md).
