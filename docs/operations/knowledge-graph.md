# Knowledge graph (INPROOM-34)

INPROOM builds a graph of each project: issues, epics, components, versions, Confluence pages and attachments, uploaded documents, code files, code owners and commits, and how they connect.
- **Every edge carries its evidence**: a citation that resolves through `POST …/provenance/resolve`.
- **Every edge says how INPROOM knows**: `validated` (a source-system field states it), `inferred` (read from text), or `manual` (added by a curator).
- **Curators** can confirm, reject or correct any edge. Their decisions survive every rebuild.

The graph is a **projection** of the content store, like chunks and vectors: it is rebuilt from content and deleted with it. Only curators' reviews are durable user data.

## What is extracted

| Node type | Key | From |
|---|---|---|
| `jira_issue` | `jira:PAY-17` | Each Jira issue (attributes: canonical `issue_type`, `status`, `labels`) |
| `jira_component`, `jira_version` | `component:PAY:Billing`, `version:PAY:4.2` | Named by issues |
| `confluence_page`, `attachment` | `confluence:101`, `confluence:att:9001` | Each page and attachment |
| `document` | `doc:<document id>` | Each uploaded document |
| `code_file` | `file:<repo>:<branch>:<path>` | Each indexed file |
| `owner`, `commit` | `owner:@acme/payments`, `commit:<sha>` | CODEOWNERS, and the commit a file was indexed at |

| Edge | Origin | Evidence |
|---|---|---|
| `child_of` | validated | Jira `parent` / `subtasks`; Confluence page tree |
| `in_epic` | validated | The mapped epic-link field |
| `blocks`, `relates`, `duplicates`, `clones`, … | validated | Jira issue links, in Jira's direction (outward: this → other) |
| `in_component`, `fix_version`, `affects_version` | validated | Jira `components`, `fixVersions`, `versions` |
| `attached_to` | validated | The Confluence attachment's page |
| `owned_by`, `at_commit` | validated | CODEOWNERS at the indexed commit; the file's commit |
| `mentions` | inferred | A Jira key, a Confluence page link or a file path in the item's text. The evidence is the chunk citation, the matched text and an excerpt |
| `duplicates` | inferred | Two items with identical content (confidence 0.95) |

**Mention confidence:**
- a Jira key of one of the project's Jira projects: **0.9**;
- a key with an unknown prefix: **0.5** (standards such as UTF-8, SHA-256 and ISO-8601 are ignored);
- a Confluence page link: **0.9**;
- a known file path: **0.8**, to every file at that path;
- an unknown code path: **0.4**, as a `path:` reference.

**Not modelled yet:** requirements (E07), test results and builds (CI integrations), commit history (only indexed branch heads exist today), and code symbols (use code search). These arrive with their sources.

## How it runs

- The derive job queues one **`graph` job** per project (coalesced) whenever it processes items.
- The job re-extracts an item only when something changed:
  - its current revision;
  - the extractor version (`graph@1`);
  - its chunks, if they were built after its last extraction.
- Each item is replaced in **one transaction**, so re-runs, retries and crashes never duplicate.
- The job requeues itself after 200 items.

**Deletion:**
- Retention removes purged revisions' rows in the purge itself.
- Items that leave the store lose their rows on the next run.
- **Withheld items keep their rows but are hidden** (see below), so a restored item's graph returns exactly as it was.

**Natural keys:**
- Edges point at keys, not rows, so a target that arrives later resolves without re-extraction.
- A key that no item carries is an **unresolved reference**. It is shown and flagged, never invented as a node.
- The same key from several items is **one node** listing every item (`items`, `asserted_by`).

## Visibility

Every read applies the project's visibility rule ([knowledge-propagation.md](knowledge-propagation.md)):
- A node is shown when at least one of its occurrences belongs to a visible item.
- An edge is shown when the item that stated it is visible and neither endpoint is hidden. An edge to an item that exists but is **hidden** (restricted, deleted, paused, over the classification cap) is hidden too, so a link never reveals a withheld item.
- An endpoint no item carries at all is **unresolved**.
- Several items stating the same edge are merged: `validated` wins, the best confidence applies, and evidence is combined (at most 10 entries).

Another tenant's or project's graph is a 404, as for every project route.

## API

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/v1/projects/{id}/graph/nodes` | project.read | `?type=&q=&limit=&cursor=`: nodes with `items`, `asserted_by` and `degree` |
| GET | `/v1/projects/{id}/graph/nodes/{key}` | project.read | A node with its `outgoing` and `incoming` edges. `?include_rejected=true` shows rejected ones |
| GET | `/v1/projects/{id}/graph/edges` | project.read | `?kind=&origin=&status=&unresolved=&limit=&cursor=` |
| GET | `/v1/projects/{id}/graph/status` | project.read | Counts by node type, edge kind, origin and status; orphans; unresolved references; `pending` items; extractor |
| POST | `/v1/projects/{id}/graph/edges/{edge_id}/review` | project.graph.curate | `{action: confirm \| reject \| correct, to?, reason}` |
| POST | `/v1/projects/{id}/graph/edges` | project.graph.curate | A manual edge: `{from_key, kind, to_key, reason}`. Returns 201, and restating it is idempotent |

`project.graph.curate` belongs to project admin, product owner, tech lead and QA lead. Denials are audited, as for every permission.

## Reviewing edges

| Action | Effect |
|---|---|
| `confirm` | The edge's `status` becomes `confirmed`. Its origin stays visible, for example `inferred` but confirmed |
| `reject` | The edge is hidden from every read (list it with `?status=rejected`). Extraction may find it again, but it **stays rejected** |
| `correct` | Rejects the edge and adds a **manual** edge of the same kind to `to`, which must be a visible node |
| manual edge | An edge only a curator states (`origin: manual`, `status: confirmed`). A 409 `GRAPH_EDGE_EXISTS` means extraction already states it, so confirm it instead |

**How reviews are stored:**
- Reviews are keyed by the edge's **natural key** (from, kind, to), not by its id.
- Edge ids follow the stating rows, so **re-read ids after a rebuild**.
- A manual edge's id is negative: minus its review's id.

**Audit:** every review or manual edge writes `graph.edge.reviewed` or `graph.edge.created` to the audit log, with the edge, reason and actor.

## Errors

| Code | Meaning |
|---|---|
| `GRAPH_NODE_NOT_FOUND` (404) | No visible content of the project carries the key |
| `GRAPH_EDGE_NOT_FOUND` (404) | No such edge id, or its content is withheld or gone. Re-read the node |
| `GRAPH_EDGE_EXISTS` (409) | Extraction already states this edge |
| `GRAPH_EDGE_MANUAL` (409) | Confirming a manual edge: it is confirmed already |
| `GRAPH_CORRECTION_SAME_TARGET` (409) / `GRAPH_CORRECTION_TARGET_REQUIRED` (422) | A correction must name a different target |
| `GRAPH_EDGE_SELF` (422) | An edge needs two different nodes |
| `PROJECT_ARCHIVED` (409) | Archived projects are read-only |

## Operating

| Symptom | Cause and action |
|---|---|
| `status.pending` stays above 0 | The graph job is queued or failing. Check the worker and sync jobs of kind `graph` (store outages retry with backoff) |
| Many `unresolved_keys` with one prefix | A Jira project or space the project does not hold. Select it as a source, or ignore it |
| A link should not exist | Reject it, or correct it. The decision holds through rebuilds |
| Many orphans | Items nothing links to. Usually expected for standalone documents |
| After changing extraction rules | Bump `EXTRACTOR` in `app/content/graph.py`. Every item is re-extracted on the next run, and reviews keep applying |

**Telemetry:** each job run logs `knowledge graph project=<id> {"items", "nodes", "edges", "dropped", "more"}`.

Project memory snapshots render approved graph links: [project-memory.md](project-memory.md).

Related: [retrieval.md](retrieval.md), [vector-index.md](vector-index.md), [provenance.md](provenance.md), [knowledge-propagation.md](knowledge-propagation.md), [code-index.md](code-index.md).
