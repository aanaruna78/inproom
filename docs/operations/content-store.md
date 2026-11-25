# Content store and source revisions (INPROOM-27)

INPROOM keeps an exact copy of every Confluence page body and Jira issue record it syncs. Each copy is a versioned **source revision**. Anything derived from a revision (text now; parsed sections, chunks and embeddings later) is a **derivative**, and each derivative names the one revision it was built from. Derivatives can be invalidated and rebuilt at any time.

## Where things live

| What | Where |
|---|---|
| Original bytes and derivative outputs | S3-compatible object store, bucket `inproom-content`, key `tenants/{tenant}/blobs/sha256/{h[0:2]}/{h}` |
| Item identity, status, ACL | `source_objects` (one row per project source item: Confluence page id, Jira issue id) |
| Revision chain | `source_revisions` (seq 1..n, parent revision, sha256, size, media type, provider version, ACL snapshot, fetching job, `purged_at`) |
| Derivatives | `derivatives` (revision, producer@version, status, output sha256, error) |

**Storage rules:**
- **Blobs are content-addressed per tenant.** Identical bytes are stored once within a tenant and never shared between tenants.
- **Every read is checked against the sha256** in the blob's key. A mismatch is `BLOB_CORRUPT`, and the bytes are never served or used.
- **All three tables use forced row-level security.**

### Configuration

| Setting | Default | Meaning |
|---|---|---|
| `INPROOM_OBJECTSTORE_URL` | `http://localhost:18333` | S3 endpoint (SeaweedFS locally; MinIO or S3 in deployments) |
| `INPROOM_OBJECTSTORE_BUCKET` | `inproom-content` | Created on first use if missing |
| `INPROOM_OBJECTSTORE_ACCESS_KEY` / `_SECRET_KEY` | unset | Credentials (required outside local) |
| `INPROOM_OBJECTSTORE_TIMEOUT_S` | 5 | Connect timeout (reads get 4×) |
| `INPROOM_CONTENT_MAX_BYTES` | 26214400 | Larger items are recorded as `TOO_LARGE`, without their bytes |
| `INPROOM_CONTENT_PURGE_INTERVAL_SECONDS` | 86400 | How often each project's retention purge runs |
| `INPROOM_CONTENT_ORPHAN_GRACE_SECONDS` | 86400 | Unreferenced blobs younger than this are left alone |

## How content gets in

```mermaid
sequenceDiagram
  participant Sync as Confluence / Jira sync job
  participant S3 as Object store
  participant DB as PostgreSQL
  participant D as derive job
  Sync->>DB: shared tenant blob lock
  Sync->>S3: put blob if absent (content address)
  Sync->>DB: lock the item row; same bytes as current means no-op, else revision n+1 (parent n)
  Sync->>DB: older derivatives SUPERSEDED, new ones PENDING, derive job queued, outbox source.revision.created
  Note over Sync,DB: catalog, revisions and events commit together, or not at all
  D->>S3: read the revision (sha256 verified), store the output
  D->>DB: derivative READY
```

**Confluence:**
- **CURRENT pages** store their storage-format body. The listing already carries it, so there are no extra provider calls.
- **RESTRICTED, EXCLUDED and DELETED pages** only change the item's status.
- **Restricted pages that were never in scope are never stored.**

**Jira:**
- **Fetched issues** are stored as canonical JSON: a fixed field set plus the mapped custom fields, with sorted keys.
- **Normalised details:**
  - linked, parent and sub-task issues are reduced to id and key;
  - avatars, `self` URLs and e-mail addresses are dropped.
  - A linked issue's change therefore never counts as a new revision of this one.
- **Deleted issues** mark the item DELETED.

The job results show `content: {revisions, unchanged, withheld}`.

## Reading content

| Route | Permission | Notes |
|---|---|---|
| `GET /v1/projects/{id}/content` | project.read | Items with their current revision; filter by `kind`, `source_id`, `status` |
| `GET /v1/projects/{id}/content/{kind}/{source_id}/{external_id}` | project.read | Status, ACL and full lineage (`parent_seq`, `stored`) |
| `GET …/revisions/{seq}/raw` | project.read | Original bytes (ETag = sha256) |
| `GET …/revisions/{seq}/derivatives` | project.read | Everything derived from that revision, history included |
| `POST /v1/projects/{id}/content/invalidate` | project.source.manage | `{kind, source_id, external_id?, producer?, reason}` → 202; audited |

**Raw bytes** are served only while the item is `CURRENT`, its source is `ACTIVE` and the revision's bytes are stored. Otherwise the API returns **409 `CONTENT_WITHHELD`** with a `reason`, and each refusal is audited as DENIED:

| reason | Meaning | What to do |
|---|---|---|
| `DELETED` / `EXCLUDED` / `RESTRICTED` | The item left the sync scope | Nothing, or change the space's filters |
| `SOURCE_PAUSED` / `SOURCE_REMOVED` | The source is paused (access lost, connection revoked, excluded) or no longer selected | Fix the source on the Sources page |
| `PURGED` | Removed by the retention policy | Nothing; the lineage stays |
| `NOT_STORED` | Larger than `INPROOM_CONTENT_MAX_BYTES` | Raise the limit if the content is needed |
| `BLOB_MISSING` / `BLOB_CORRUPT` | The object store lost or altered the bytes | Check the object store; **Reconcile now** re-fetches changed items |

## Derivatives

- **Lifecycle:** `PENDING → READY | FAILED`. A new revision makes the older revisions' derivatives `SUPERSEDED`. Invalidation makes them `INVALIDATED` and queues fresh `PENDING` rows.
- **No duplicate work:** a partial unique index allows only one live row per revision and producer@version.
- **Determinism:** producers are pure functions, so a rebuild of the same revision is byte-identical.
- **Consumers** must use `app.content.derivatives.live()`: READY output of the current revision of a CURRENT item whose source is ACTIVE.
- **Producers:**
  - `text@1` (built in) produces plain text from Confluence storage XHTML and Jira records.
  - Parsers (E03-S02), the code index (E03-S03), chunks and embeddings (E04) are added as further producers.
  - Bumping a producer's version and invalidating rebuilds everything.
- **FAILED codes:** `PRODUCER_FAILED`, `NOT_STORED`, `BLOB_MISSING`, `BLOB_CORRUPT` and `UNKNOWN_PRODUCER`. They are explained in the error catalog. Invalidate after fixing the cause.

## Retention

Each project's settings carry `sources.retention`:
- **`keep_revisions`** (default 10): the newest revisions of each item are always kept.
- **`superseded_days`** (default 90): an older revision is purged this long after it was replaced.
- **`deleted_days`** (default 30): a deleted item's content is purged this long after deletion. Items whose source is removed from the project count as deleted from that moment.

**The daily `purge` job per project:**
1. Sets `purged_at` on the released revisions and their derivatives. The lineage rows stay.
2. Deletes a blob only when **no unpurged revision or derivative anywhere in the tenant** still references it.
3. Never purges the current revision of a live item.
4. Collects **orphan blobs** (nothing references them, and they are older than the grace period), such as a blob written by a sync that then rolled back.
5. Emits `content.purged`, and the job result carries the counts.

**Concurrency:**
- Syncs and derive jobs hold a **shared** per-tenant advisory lock from writing a blob until they commit.
- Blob deletion takes the lock **exclusively** and re-checks references.
- So a blob that a concurrent sync is about to reuse is never deleted.

## Failure and recovery

| Situation | Behaviour |
|---|---|
| Object store down during a sync | The sync job fails with `OBJECTSTORE_UNAVAILABLE` and retries with backoff. The catalog, revisions and events roll back together, so nothing partial is visible. |
| Worker dies after writing a blob, before the commit | No row references the blob. The retry records the revision once and reuses the blob. If nothing ever does, the purge collects the orphan. |
| Object store down during a derive job | The job retries, and derivatives stay `PENDING`. |
| Object store down on a raw read | `503 OBJECTSTORE_UNAVAILABLE` with `Retry-After`. |
| Lost or corrupted blob | Reads are withheld (`BLOB_MISSING` / `BLOB_CORRUPT`) and audited. Derivatives built from it are `FAILED`. |
