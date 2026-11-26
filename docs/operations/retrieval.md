# Hybrid retrieval (INPROOM-33)

`POST /v1/projects/{id}/knowledge/retrieve` finds the passages that answer a query. It combines **keyword matches** (exact words, identifiers, issue keys, error codes) with **semantic matches** (vector similarity, [vector-index.md](vector-index.md)). It removes duplicates, explains each result, and says plainly when the project holds **no good evidence**.

Only **visible current** content is ever returned, in every mode: the same visibility rule as every read path ([knowledge-propagation.md](knowledge-propagation.md)). Filters run inside each candidate query, so withheld content can never take a slot.

`POST …/knowledge/search` (INPROOM-32) remains the plain vector search, with cosine scores. Build new features on `/retrieve`.

## Request

```json
{
  "query": "ledger lock E1042 -staging \"time out\"",
  "mode": "hybrid",
  "top_k": 10,
  "kinds": ["confluence", "upload"],
  "source_ids": ["…"],
  "max_per_document": 3,
  "changed_after": "2026-11-01T00:00:00Z",
  "max_age_days": 30,
  "exclude_stale": false,
  "include_unsupported": false
}
```

| Field | Meaning |
|---|---|
| `query` | Words and `"quoted phrases"` are OR-ed, and results matching more of them rank higher. `-word` or `-"a phrase"` excludes, in **every** mode. `or` is accepted and ignored. Stop words alone (`the`, `of`) are ignored and listed in `ignored_terms`. A query with nothing searchable (for example only exclusions) is `422 QUERY_HAS_NO_TERMS` |
| `mode` | `hybrid` (the default), `keyword` or `semantic` |
| `max_per_document` | At most this many passages of one item (default 3), so one long page cannot fill the list |
| `changed_after`, `max_age_days` | Only items INPROOM saw change after that time. The time is when the current revision was retrieved, since a revision is only created when content changes. If both are given, the stricter applies |
| `exclude_stale` | Drop items whose freshness is `stale`: their source's sync is failing or overdue, or the provider changed them after retrieval. Without it, stale items are returned and marked |
| `include_unsupported` | Also return hits that are not evidence (`explanation.supported = false`). Use it for debugging only |

## Response

```json
{
  "mode": "hybrid",
  "evidence": {"status": "sufficient", "reason": null, "detail": "3 supported result(s)", "supported": 3},
  "model": "voyage-3.5",
  "degraded": [],
  "query_terms": ["ledger", "lock", "E1042", "time out"],
  "excluded_terms": ["staging"],
  "ignored_terms": [],
  "items": [{
    "citation": {"kind": "upload", "source_id": "…", "external_id": "…", "seq": 1, "anchor": "…"},
    "title": "…", "path": ["Incident 2026-03"], "text": "…", "retrieved_at": "…",
    "provenance": {"freshness": "current", "trust": "trusted", "…": "…"},
    "also_in": [{"citation": {"…": "…"}, "title": "copy-of-incident.md"}],
    "explanation": {
      "keyword": {"rank": 1, "score": 0.42, "terms": ["ledger", "lock", "E1042"]},
      "semantic": {"rank": 3, "similarity": 0.71},
      "rrf": 0.032266,
      "supported": true
    }
  }]
}
```

- **Citations** resolve through `POST /v1/projects/{id}/provenance/resolve`, as for every passage.
- **`also_in`:** identical text (the same sha256) is returned once, at its best position. The other places it appears are listed there.

## How ranking works

1. **Keyword candidates** (the top 50, `INPROOM_RETRIEVAL_CANDIDATES`) come from Postgres full-text search on `knowledge_chunks.search_tsv` (migration 0024, GIN index).
   - The title, external id and heading path count more (weight A) than the text (weight B).
   - Each word matches **stemmed** ("restart" finds "Restarting") or **verbatim** (`PAY-17`, `get_user`, `E1042`).
   - The score is `ts_rank_cd`.
2. **Semantic candidates** (the top 50) are the nearest chunks under the **active embedding model** (cosine).
3. **Reciprocal Rank Fusion:** `rrf = Σ 1 / (k + rank)` over the lists a chunk appears in (`k` = 60, `INPROOM_RETRIEVAL_RRF_K`). Only ranks are used, so cosine and `ts_rank` never need calibrating against each other, or against a new model. A chunk found by both lists rises.
4. **Dedupe** (identical text once, with `also_in`), then the **per-document cap**.
5. **Evidence gate:** a hit is **supported** when it matched at least one query word or phrase, or its cosine similarity is at least `INPROOM_RETRIEVAL_MIN_SIMILARITY`. Unsupported hits are dropped.

## Evidence: never answer from nothing

`evidence.status = insufficient` means the results must **not** be used to answer (E05 and E07 consumers refuse, or ask for more). `items` is then empty unless `include_unsupported` is set.

| Reason | Meaning | What to do |
|---|---|---|
| `NO_CONTENT` | Nothing visible and chunked exists in this scope (kinds and sources) | Select sources, wait for sync and derive (`GET …/knowledge/index`), or check the visibility rules |
| `ALL_FILTERED` | Matching content exists, but `changed_after`, `max_age_days` or `exclude_stale` removed all of it | Relax the freshness filter, or fix the stale source's sync |
| `NO_MATCH` | No candidate at all: no keyword match, and no semantic candidates (nothing embedded yet, or semantic degraded) | Rephrase, or check `pending` in the index status |
| `LOW_RELEVANCE` | Candidates exist, but none matched a term or reached the similarity floor | The project probably does not cover this. Do not answer |

When `degraded` contains `semantic`, `detail` says that only keyword matches were considered.

## Configuration

| Setting | Default | Notes |
|---|---|---|
| `INPROOM_RETRIEVAL_MIN_SIMILARITY` | `0.35` | The cosine floor for a semantic-only hit to count as evidence. **It depends on the model.** 0.35 suits `local-hash-v1`. Hosted models score unrelated text differently, so re-tune with `make eval-retrieval` when switching models |
| `INPROOM_RETRIEVAL_CANDIDATES` | `50` | Candidates per list. More improves recall at some latency cost |
| `INPROOM_RETRIEVAL_RRF_K` | `60` | Higher flattens the rank contributions |

## Failure handling

| Situation | Behaviour |
|---|---|
| Embedding provider down or rate limited | `hybrid` falls back to keyword only, with `degraded: ["semantic"]` and `model: null`. `semantic` gives `503 EMBEDDING_UNAVAILABLE` with `Retry-After`. Retrieval writes nothing, so retrying is always safe |
| Query has no searchable terms | `422 QUERY_HAS_NO_TERMS` |
| No permission or another tenant's project | `404` (denials are audited, as for every project route) |

## Quality: the golden retrieval set

`make eval-retrieval` loads `apps/api/tests/data/golden_retrieval.json` into a fresh test project. That set contains runbooks, ADRs, incident and issue notes, Confluence pages and code, plus **leak bait**: a restricted page, a deleted upload, and another project's document. It writes `apps/api/build/retrieval-eval.md`, which gives recall@5, MRR, the correct evidence verdicts, and the leaks for each mode.

The test asserts four things:
- zero leaks in every mode;
- hybrid recall@5 ≥ 0.9 and MRR ≥ 0.8;
- hybrid recall at least that of either single mode;
- every answerable query is `sufficient` and every unanswerable one `insufficient`.

Baseline (local-hash-v1, golden set v1):

| Mode | recall@5 | MRR | evidence correct | leaks |
|---|---|---|---|---|
| keyword | 1.0 | 1.0 | 19/19 | 0 |
| semantic | 0.312 | 0.312 | 9/19 | 0 |
| hybrid | 1.0 | 0.969 | 19/19 | 0 |

The local embedder hashes words, so its semantic-only results are weak, and most fall under the similarity floor. That is expected, and why hybrid is the default. To evaluate a hosted model:
1. Set the `INPROOM_EMBEDDING_*` settings for the test run.
2. Run `make eval-retrieval`.
3. Tune `INPROOM_RETRIEVAL_MIN_SIMILARITY` until the unanswerable queries come back `insufficient` and the answerable ones `sufficient`.

When you add a query, add its expected hits too, and bump `version`.

## Telemetry

Each call logs one line, `knowledge retrieve project=<id> {…}`, containing:
- mode, model, degraded, evidence (the reason or `SUFFICIENT`);
- candidate counts per list, returned, supported;
- the names of the filters used, and the latency in `ms`.

The query text is **never** logged.

**Watch for:**
- a rising share of `LOW_RELEVANCE` or `NO_MATCH`: coverage gaps, or a similarity floor set too high for the model;
- frequent `degraded`: the embedding provider.

Related: [vector-index.md](vector-index.md), [provenance.md](provenance.md), [knowledge-propagation.md](knowledge-propagation.md).
