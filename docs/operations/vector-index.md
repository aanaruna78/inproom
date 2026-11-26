# Chunks, embeddings and the vector index (INPROOM-32)

INPROOM splits every document, Confluence page, Jira issue and source file into **citable chunks**, embeds each chunk with the configured model, and answers **semantic search** over them. Only content the project may see is ever returned.

Chunks and vectors are **projections** of the content store (ADR-002). They can be rebuilt at any time, and they are deleted with the content.

## Pipeline

1. **Chunking (`chunks@1`).** This is a pure producer: the same input always gives the same chunks.
   - **Documents** (uploads, Confluence, Jira) are chunked from their READY `parse@2` output, along sections:
     - each chunk carries the heading path as context;
     - about 400 tokens per chunk, with about 15% sentence overlap;
     - a table stays whole when it fits; otherwise it is split by rows, with the header repeated.
   - **Source files** are chunked along their `symbols@1` definitions. A large class is split into its methods, and module code becomes line windows.
   - Every chunk has a **citation** `{kind, source_id, external_id, seq, anchor}`, which resolves through `POST /provenance/resolve`.
   - If a document's parse failed, its chunks are `UPSTREAM_FAILED`. The parse failure is the one to fix (see the parse failures list).
2. **Projection.** READY chunk outputs become rows in `knowledge_chunks`, in the same transaction. Rows leave when their derivative does: superseded, invalidated or purged.
3. **Embedding.**
   - The derive job queues one **embed** job per project, coalesced.
   - The job embeds each distinct chunk text of **visible current** content that has no vector for the active model.
   - Vectors are keyed by **(tenant, model, sha256 of the text)**, so identical text is embedded once per tenant. Vectors are **never** shared across tenants.
   - Each batch commits on its own, so retries never duplicate.
4. **Search.** `POST /v1/projects/{id}/knowledge/search` embeds the query with the **active model** and returns the top-k chunks. It considers only:
   - the **current revision**;
   - of **visible** items (the same rule as every read path: [knowledge-propagation.md](knowledge-propagation.md));
   - with vectors of the **active model**.

## Configuring the embedding provider

| Setting | Default | Notes |
|---|---|---|
| `INPROOM_EMBEDDING_PROVIDER` | `local` | `local` (deterministic hashing; no network; lexical quality only) or `openai-compatible` |
| `INPROOM_EMBEDDING_MODEL` | `local-hash-v1` | The **active model**. Changing it is a model switch (see below) |
| `INPROOM_EMBEDDING_DIMENSIONS` | `256` | Must equal what the model returns (otherwise `EMBEDDING_REJECTED`). HNSW supports up to 2000 |
| `INPROOM_EMBEDDING_BASE_URL` | — | For example `https://api.voyageai.com/v1`, `https://api.openai.com/v1`, `http://localhost:11434/v1` (Ollama) |
| `INPROOM_EMBEDDING_API_KEY` | — | A deployment secret. It is sent only as `Authorization: Bearer …`, and never logged or returned |
| `INPROOM_EMBEDDING_ALLOWED_HOSTS` | `api.voyageai.com, api.openai.com` | The egress allowlist for the base URL. Add an explicit origin such as `http://localhost:11434` for a local server |
| `INPROOM_EMBEDDING_INPUT_TYPE` | `false` | `true` sends `input_type: document \| query` (Voyage AI recommends it). Leave it off for APIs that reject unknown fields |
| `INPROOM_EMBEDDING_BATCH_SIZE` / `_TIMEOUT_SECONDS` | `64` / `30` | Per provider call |

**Examples:**
- **Voyage AI:** provider `openai-compatible`, base URL `https://api.voyageai.com/v1`, model `voyage-3.5`, dimensions `1024`, input type `true`.
- **OpenAI:** base URL `https://api.openai.com/v1`, model `text-embedding-3-small`, dimensions `1536`.
- **Ollama (self-hosted, no data leaves your network):**
  - base URL `http://localhost:11434/v1`;
  - model `nomic-embed-text`, dimensions `768`;
  - allowed hosts must include `http://localhost:11434`.

### Vector index per model

Each model has its own partial HNSW cosine index:

```sql
CREATE INDEX IF NOT EXISTS ix_chunk_embeddings_<model> ON chunk_embeddings
  USING hnsw ((embedding::vector(<dimensions>)) vector_cosine_ops) WHERE model = '<model>';
```

The embed job creates it on first use when the database login owns the table, which is the case locally. Where the runtime login is not the owner, run the statement at deploy time as the migration owner. Until it exists, search still works by exact scan, just slower. The index for `local-hash-v1` is created by migration 0023.

## Switching models

1. Set the new model, dimensions and URL, and restart the API and the worker. Search now uses the new model.
2. Run `POST /v1/projects/{id}/knowledge/reindex {"scope": "embeddings", "reason": "…"}` for each project. It returns 202, is coalesced, and is audited as `knowledge.reindex`.
3. Watch `GET /v1/projects/{id}/knowledge/index`: `pending` drops to 0, and `models` lists both models while they coexist. The old model's vectors are never used by search.

> Between steps 1 and 3, search returns fewer results for a project, because only chunks already embedded with the new model match. Plan the switch in a quiet period, or reindex right after restarting.

## Operating

| Route | Use |
|---|---|
| `GET …/knowledge/index` | Active provider, model, version and dimensions; `chunks`, `embedded`, `pending`, `failed`; chunk derivatives by status; vectors per model |
| `GET …/knowledge/index/failures` | Chunks the provider rejected, with a citation, code, detail and attempts |
| `POST …/knowledge/reindex` `{"scope": "embeddings"}` | Clears this project's failures for the active model, and embeds whatever is missing |
| `POST …/knowledge/reindex` `{"scope": "chunks"}` | Rebuilds every chunk derivative (for example after a chunker upgrade). Unchanged text reuses its vector |

**Deletion.** When retention purges content, the tenant's vectors whose text no chunk carries any more are deleted in the same purge (`vectors_deleted` in the `content.purged` event). A vector still used by any project of the tenant stays.

## Failure handling

| Code | Meaning | What happens |
|---|---|---|
| `EMBEDDING_UNAVAILABLE` | Timeout, 5xx, or the provider isn't configured | The embed job retries with backoff, and nothing is recorded against chunks. Search returns `503` with `Retry-After` |
| Rate limited (429) | The provider is throttling | The job waits for `Retry-After` without using an attempt |
| `EMBEDDING_CREDENTIAL_INVALID` | The API key was rejected | Rotate `INPROOM_EMBEDDING_API_KEY`. The job keeps retrying |
| `EMBEDDING_REJECTED` | Another 4xx, a malformed response, or wrong dimensions | The batch is retried input by input. Only the offending chunk is recorded (see failures), and the rest are embedded |
| `UPSTREAM_FAILED` | The document's parse failed | Fix the parse (parse failures list). Chunks follow automatically |

## Troubleshooting

| Symptom | Cause and action |
|---|---|
| Search returns nothing after a model switch | Vectors for the new model don't exist yet. Run a reindex, then watch `pending` |
| `failed` > 0 with "returned N dimensions; configured M" | `INPROOM_EMBEDDING_DIMENSIONS` doesn't match the model. Fix it, then reindex embeddings |
| Search is slow on a large project | The model's HNSW index is missing. Create it at deploy time (see above) |
| Results seem lexical rather than semantic | The `local` provider is active. Configure an `openai-compatible` model |
| A restricted or deleted page never appears | Working as intended: search applies the visibility rule ([knowledge-propagation.md](knowledge-propagation.md)) |

Related: [content-store.md](content-store.md), [provenance.md](provenance.md), [knowledge-propagation.md](knowledge-propagation.md), [code-index.md](code-index.md).
