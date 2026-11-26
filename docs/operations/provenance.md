# Provenance, trust and freshness (INPROOM-30)

Every passage INPROOM cites can be traced back to its source. A **provenance record** answers four questions:
- where the passage came from (source, item, revision, URL);
- when it was retrieved, and when the source last changed it;
- how far to trust it (trust tier and classification);
- whether it is still current (freshness, conflicts and duplicates).

Records are **computed at read time** from the content store, so nothing extra is stored or kept in sync. The only new table is `source_policies` (migration `0019`).

## APIs (project.read)

| Route | Use |
|---|---|
| `POST /v1/projects/{id}/provenance/resolve` | Resolve up to **100** citations `{kind, source_id, external_id, seq, anchor?}`. Each citation succeeds or fails **on its own**, and the batch is always `200` (more than 100 citations gives `422`). |
| `GET /v1/projects/{id}/content/{kind}/{source_id}/{external_id}/revisions/{seq}/provenance?anchor=` | The record for one revision. Purged and not-stored revisions still return their metadata. |
| `GET/PUT /v1/projects/{id}/sources/{kind}/{source_id}/policy` | Read or set a source's trust and classification. `PUT` needs `project.source.manage`, `If-Match` with the current version (`"0"` for defaults) and a `reason`. |

Code search hits and parsed documents also carry a compact `provenance` summary: freshness, reason codes, trust, classification, derivation and `retrieved_at`.

### Per-citation outcomes

| `error.code` | Meaning | Audited as refusal |
|---|---|---|
| none | `record` is returned | no |
| `NOT_FOUND` | There is no such item or revision **in this project**. Another tenant's or another project's citation looks exactly like this. | yes |
| `CONTENT_WITHHELD` | The item exists but the caller may not read it. `reason` is the shared withholding rule (`RESTRICTED`, `DELETED`, `SOURCE_PAUSED`, `CLASSIFICATION_EXCEEDS_PROJECT`, …). No title, URL or hash is returned. | yes |
| `ANCHOR_NOT_FOUND` | The revision is parsed, but it has no such section or symbol. | no |
| `INVALID_CITATION` | The citation is malformed (an unknown kind, a missing field, `seq < 1`, …). `detail` names the field. | no |

A call with any refusals writes **one** `provenance.resolve` audit event with outcome `DENIED` (for example "3 of 5 citations refused: NOT_FOUND, RESTRICTED"). It never writes one event per citation. `counts` in the response gives the same breakdown (`resolved`, `withheld`, `not_found`, `anchor_not_found`, `invalid_citation`) for dashboards.

## The record

- **source**: kind, name (space, project key or repository), title, and `url`:
  - Confluence: `<site>/wiki/...` on Cloud;
  - Jira: the issue's web address on the connected site;
  - git: a permalink pinned to the verified commit.
- **revision**: `seq`, `provider_version`, `retrieved_at`, `verified_ref` (git), `content_stored`.
- **modified**: when and by whom the provider says the item last changed.
- **trust**: tier, classification, `origin` (`default` or `policy`).
- **access**: what the item was fetched under (space, project or branch), and its current status.
- **derivation**: `verbatim` (the whole item), `extracted` (a parsed section or code symbol, with its producer and version), or `inferred`.
- **passage** (when there is an anchor): a section (heading, path, page) or a symbol (name, kind, lines).
- **completeness**: `score`, plus `missing[]`, which gives each unknown field with the reason it is unknown. **Unknown metadata is listed and never invented.**
- **freshness**, **conflicts**, **duplicates** (see below).

## Trust and classification

| Kind | Default tier | Default classification |
|---|---|---|
| Confluence, Jira, git | `trusted` | `internal` |
| Upload | `reference` | `internal` |

- **Tiers** are `authoritative > trusted > reference > untrusted`. They are informational, for ranking and display.
- **Classification is enforced.** A source classified above the project's `settings.sources.max_classification` is withheld wherever content is served, with reason `CLASSIFICATION_EXCEEDS_PROJECT`:
  - raw content, documents and code files;
  - provenance;
  - code search;
  - downstream derivative consumers.
- Raising the cap, or lowering the source's classification, restores access at once. Nothing is deleted.
- Each change writes the audit event `source.policy.update` and the outbox event `source.policy.changed` (from, to, reason). Sending an identical update again changes nothing.

## Freshness

| Status | When |
|---|---|
| `current` | This is the item's latest revision, and no stale signal applies. |
| `superseded` | A newer revision exists (`NEWER_REVISION`). |
| `stale` | It is the latest revision, but a signal says it may be out of date. |

| Signal | Cause |
|---|---|
| `SOURCE_SYNC_FAILING` | The source's latest syncs failed. |
| `SOURCE_SYNC_STALE` | No successful sync for more than 3 sync intervals. |
| `CODE_BRANCH_BEHIND` | The branch head moved past the verified commit, and the code index hasn't caught up. |
| `PROVIDER_HAS_NEWER_VERSION` | The Confluence catalog has a newer page version than the stored one. |
| `PROVIDER_CHANGED_AFTER_RETRIEVAL` | The provider's modified time is later than the retrieval time. |

`freshness.source_sync` gives the source's health and `data_as_of`. A source that was synced only through the API (for example at selection) counts its `last_synced_at` as a success.

**Conflicts** are other *visible* current items in the project that claim the same document but have different content: the same normalized title for documents, or the same path across branches and repositories for code. **Duplicates** are the same items with identical content. Each list has at most 20 entries. Items the caller can't read are never listed.

## Troubleshooting

| Symptom | Cause and action |
|---|---|
| The record has `passage: null`, and `missing` says "the parsed document is unavailable (OBJECTSTORE_UNAVAILABLE)" | The object store is down. The rest of the record is still served from PostgreSQL. It recovers by itself; see [content-store.md](content-store.md#failure-and-recovery). |
| `passage: null`, with "not parsed yet" or "symbols are not built yet" | The derivative is still `PENDING`. Check the derive queue, or the parse failures list (`GET .../content/parse-failures`). |
| Everything from one source is `CONTENT_WITHHELD`, with `CLASSIFICATION_EXCEEDS_PROJECT` | The source's classification is above the project cap. Check `GET .../policy`: `within_project_cap` is false. |
| Many records are `stale` with `SOURCE_SYNC_FAILING` | The source sync is failing. See [sync-worker.md](sync-worker.md). |
| `CODE_BRANCH_BEHIND` persists | The code job for that branch isn't completing. See [code-index.md](code-index.md#troubleshooting). |
| A client gets `INVALID_CITATION` | It is sending malformed citations. `detail` names the field. |

Withheld content and the change feed consumers follow: see [knowledge-propagation.md](knowledge-propagation.md).
