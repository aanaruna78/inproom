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

Repository code (files, symbols, ownership, code search) has its own runbook: [code-index.md](code-index.md).

## Documents and parsing (INPROOM-28)

Every stored revision of a supported type gets a **`parse@2`** derivative (kind `document`): a structured document whose sections have stable anchors, so citations can point at a page or section.

**Anchors** are unique within a document:
- A heading's anchor is its parent heading's anchor, then `--`, then its own slug (for example `guide--setup`). A document title is never a prefix.
- A collision takes the next free `-2`, `-3` suffix, checked against every anchor in the document.
- Inserting another branch of headings elsewhere does not change an existing heading's anchor.

`parse@1` outputs (before INPROOM-168) are rebuilt by `POST /content/invalidate` with `producer: "parse"`.
Invalidation also retires queued work for obsolete producer versions, so a pending `parse@1` job cannot
become a false failure during that rebuild. Repeating the request while the current version is pending
does not duplicate or restart its work. The parse-failures list reports the registered parser version;
obsolete failure records remain in derivative history for inspection.

| Format | How it gets in | Sections and anchors |
|---|---|---|
| PDF | upload, Confluence attachment | one per page, `page-N` |
| DOCX | upload, Confluence attachment | Title and Heading N styles; heading-path anchors; tables |
| Markdown | upload, attachment (`.md`) | headings (path anchors); GFM tables; code blocks |
| HTML, Confluence storage | upload, attachment, Confluence pages | h1–h6; tables; `ri:attachment` references |
| Plain text | upload, attachment (`.txt`) | paragraph groups of about 4,000 characters, `para-N` |
| Jira issue | Jira sync | `field-summary`, `field-description`, `field-environment`, text custom fields |

### Reading parsed documents

| Route | Permission | Notes |
|---|---|---|
| `GET …/content/{kind}/{source_id}/{external_id}/revisions/{seq}/document` | project.read | Sections, anchors, tables and attachment references resolved to stored items. The same withholding as raw bytes. 409 `NOT_PARSED` (`parse_status` PENDING, FAILED or NONE, plus the `failure` code) when not parsed. |
| `GET /v1/projects/{id}/content/parse-failures` | project.read | Items whose **current** revision failed to parse, newest first, with code and detail |

### Parse failures

| Code | Meaning | What to do |
|---|---|---|
| `PARSE_MALFORMED` | Damaged, truncated, or not what its type says (for example, not UTF-8) | Re-save or re-export the file, then upload it again |
| `PDF_ENCRYPTED` | Needs a password to open | Upload an unprotected copy |
| `NO_TEXT_LAYER` | No page has text: a scan. OCR is not available yet | Upload a text-based PDF or the source document |
| `EMPTY_DOCUMENT` | No text or tables | Check the file or page |
| `UNSUPPORTED_FORMAT` | Not a type INPROOM parses | Convert it to PDF, DOCX, Markdown, HTML or text |
| `PARSE_LIMIT_EXCEEDED` | Over 2,000 pages, 5,000 sections, 10 M characters or 100,000 table cells, or a DOCX that expands past 100 MB or 100:1 (zip bomb) | Split the document |

- **Visibility:** each failure is a `FAILED` derivative, emits `content.parse.failed`, and is counted by code in the derive job's result (`failures`).
- **Robustness:** an unexpected library error on untrusted input is reported as `PARSE_MALFORMED`; it never stops the job.
- **Partial results:** a PDF with only some text-less pages is READY with a `PAGES_WITHOUT_TEXT` warning.

### Uploads

- **Who and how:** `POST /v1/projects/{id}/documents` (multipart `file`, optional `title`, supports `Idempotency-Key`), requiring `project.content.upload`. That permission is held by project_admin, product_owner, tech_lead and qa_lead.
- **Type detection:** the type is decided from the bytes. PDF and DOCX by signature; Markdown, HTML and text by extension, and only if the bytes are UTF-8. Anything else gets 415.
- **Limits and checks:** files over `INPROOM_CONTENT_MAX_BYTES` get 413. Uploads need `upload` in the project's `sources.allowed_kinds` (409 `UPLOADS_NOT_ALLOWED`). While uploads are disabled, existing uploads are withheld (`SOURCE_DISABLED`).
- **Replace:** `PUT …/documents/{id}` with `If-Match: "<seq>"` adds a revision. Identical bytes add nothing.
- **Delete:** `DELETE …/documents/{id}` marks the item DELETED; it is withheld at once and purged by retention.

### Confluence attachments

- **What is fetched:** attachments of in-scope pages whose name suggests a parseable type and whose size fits. They become items `att:<id>` with the page as parent, take the page's ACL, and are withheld whenever the page is.
- **How they are downloaded:**
  - only from the fixed REST download path;
  - with the byte cap enforced while streaming;
  - with at most one redirect, and only to the site or `INPROOM_CONFLUENCE_ATTACHMENT_HOSTS` (default `api.media.atlassian.com`), with no credential on that hop.
- **Unchanged attachment versions** are not downloaded again.
- **At most 50 downloads per sync**; the rest are `deferred` to the next sync.
- **Skipped attachments** (unsupported type, too large, inaccessible, disguised content, download failure) are listed with the reason in the source's `last_sync.content.attachments.skipped_items`. Download failures are retried on the next sync.
- **Removed attachments** become DELETED.

## Retention

Each project's settings carry `sources.retention`:
- **`keep_revisions`** (default 10): the newest revisions of each item are always kept.
- **`superseded_days`** (default 90): an older revision is purged this long after it was replaced.
- **`deleted_days`** (default 30): a deleted item's content is purged this long after deletion. Items whose source is removed from the project count as deleted from that moment.

**The daily `purge` job per project.** The worker's schedule tick queues it (`enqueue_due_purges`) for every project that holds stored content, whatever its sources are doing: removed, paused, excluded, invalid credentials or quiet. The job:
1. Sets `purged_at` on the released revisions and their derivatives. The lineage rows stay.
2. Deletes a blob only when **no unpurged revision or derivative anywhere in the tenant** still references it.
3. Never purges the current revision of a live item.
4. Leaves orphan blobs to the tenant sweep (below).
5. Emits `content.purged`, and the job result carries the counts.

**The daily `sweep` job per tenant** (`enqueue_due_sweeps`, queued by the same schedule tick for every tenant) deletes **orphan blobs**: blobs under the tenant's prefix that no unpurged revision or derivative references and that are older than `INPROOM_CONTENT_ORPHAN_GRACE_SECONDS`. Examples are a blob written by a sync that then rolled back, a derivative superseded while it was being built, or a delete that failed half-way. The sweep is tenant-level because such a blob can be a tenant's only trace: when the very first ingestion fails, no row exists to schedule from. It emits `content.swept`.

**Concurrency:**
- Syncs and derive jobs hold a **shared** per-tenant advisory lock from writing a blob until they commit.
- Blob deletion takes the lock **exclusively** and re-checks references.
- So a blob that a concurrent sync is about to reuse is never deleted.

## Failure and recovery

| Situation | Behaviour |
|---|---|
| Object store down during a sync | The sync job fails with `OBJECTSTORE_UNAVAILABLE` and retries with backoff. The catalog, revisions and events roll back together, so nothing partial is visible. |
| Worker dies after writing a blob, before the commit | No row references the blob. The retry records the revision once and reuses the blob. If nothing ever does, even when it was the tenant's first ingestion, the daily tenant sweep collects the orphan after the grace period. |
| Object store down during a derive job | The job retries, and derivatives stay `PENDING`. |
| Object store down on a raw read | `503 OBJECTSTORE_UNAVAILABLE` with `Retry-After`. |
| Item restored after its content was purged | The same bytes are stored again as a new revision whose parent is the purged one, and its derivatives are rebuilt. |
| ACL, title or URI change with an unchanged body | The item's current ACL, title and URI are updated without a new revision (revisions keep the ACL they were fetched under), and `source.object.acl_changed` is emitted. |
| Lost or corrupted blob | Reads are withheld (`BLOB_MISSING` / `BLOB_CORRUPT`) and audited. Derivatives built from it are `FAILED`. |

Every cited passage carries a provenance record (source, revision, trust, freshness): see [provenance.md](provenance.md).

Deletions, restrictions and other invalidations reach every read path and the knowledge change feed atomically: see [knowledge-propagation.md](knowledge-propagation.md).

Retrieval chunks, embeddings and semantic search are projections of this store: see [vector-index.md](vector-index.md).
