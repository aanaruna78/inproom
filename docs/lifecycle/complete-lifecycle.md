# INPROOM — End-to-End Continuous Lifecycle Specification

**Scope:** SDLC (first) → STLC (second) → configurable enterprise business processes (later).

## Shared stage contract
Each process stage stores `stage_id`, `process_version`, `tenant_id`, `project_id`, `input_refs`, `required_evidence`, `agent_policy`, `allowed_tools`, `approval_policy`, `output_schema`, `timeout/retry`, `idempotency_key` and `audit_ref`. Every write must pass a fresh permission/policy check.

| Stage | Trigger | Core behavior | Evidence / gate |
|---|---|---|---|
| 1. Create or migrate | User starts workspace | Collect metadata, import settings, distinguish authoritative vs inferred history | Project owner and scoped connector permissions |
| 2. Connect sources | Authorized Git/Jira/Confluence links | Validate OAuth/app grants, scopes, source ACL and webhook/reconciliation | Connection status without credential exposure |
| 3. Ingest and index | Initial and incremental sync | Version originals; extract entities, code symbols, relationships, embeddings and Markdown snapshots | Provenance, source version, index freshness |
| 4. Requirements intake | User uploads/text enters change | Ground requirements, ambiguity analysis, missing info and deduplication | Cited requirement understanding |
| 5. Jira drafting | Requirement approved for decomposition | Draft epic/story tree, acceptance criteria, dependencies and risk | User reviews diffs before publish |
| 6. Jira publishing | Explicit approval | Create/update issues idempotently, capture IDs and links | Approval receipt, external object IDs |
| 7. Sprint analysis | Sprint configured or refreshed | Check readiness, dependency conflicts, scope and knowledge freshness | Explanation and confidence, unresolved blockers |
| 8. Implementation planning | Sprint item selected | Generate impacted components, approach, risks, tests and Mermaid diagrams | Human approval; Confluence publish only if allowed |
| 9. Development | Approved plan | Human implements or AI harness works in restricted sandbox | Branch/path scopes, reviewable diff, tests |
| 10. PR validation | Commit / PR linked to work item | Compare diff with acceptance criteria; inspect CI, security and test evidence; flag gaps | Never declare correctness from LLM opinion alone |
| 11. Release candidate | Approved merged work | Compose immutable candidate manifest and affected areas | Release policy and human authorization |
| 12. STLC creation | Candidate accepted for QA | Draft QA stories, test cases, regression scope and environments | QA lead approves before Jira creation/allocation |
| 13. QA verification | QA sprint execution | Manual testing, framework automation or AI-assisted harness; collect actual results | Acceptance-by-criterion evidence, defect loop |
| 14. Sign-off, release, feedback | Requirements met and policy passed | Close work, record sign-offs, deployment markers, incidents, update knowledge | Authorized sign-off, final audit and source refresh |

## Key functional requirements
**Approval semantics:** AI proposals are immutable revisions. Approval binds to an exact proposal hash, target project/branch, identity and expiry. If proposal, source version, branch head or permission changes materially, the approval is invalidated.

**Jira synchronization:** Bidirectional mapping between internal work IDs and Jira keys. Use idempotency and conflict resolution rather than overwriting human edits. Publish only approved transitions and fields; retain before/after audit.

**Confluence documentation:** Mermaid must be saved as source and rendered to a visual asset. Publish with referenced issue/source IDs and page version guards. Preserve a human-reviewed architecture decision where required.

**Git verification:** Validate the intended base branch, PR merge base, changed paths, linked issues, build checks, relevant tests, dependency changes and evidence coverage. AI confidence is advisory.

**Knowledge memory:** Markdown summaries are replayable snapshots with source links, author, timestamp, project ACL and version. They must not become a hidden alternate source of truth. Source deletion and ACL changes propagate to projections.

**Defect loop:** Failed QA creates or links a defect with reproducible environment, steps, actual/expected results and evidence; return to planning/development. Closure requires test confirmation and policy-defined human sign-off.

## Failure conditions and recovery
Handle OAuth expiry, webhook loss, rate limiting, stale revisions, graph/vector disagreement, model outage, invalid JSON, PR force-push, incomplete CI, flaky tests, connector permission loss and duplicate event delivery. Use durable retries with bounded budgets, dead-letter review and human takeover. **Fail closed** for write actions and risky state transitions.

## Baseline acceptance criteria
- Source-based, ACL-scoped analysis with citations and visible freshness.
- No Jira/Confluence/Git write before configured approval.
- Durable workflow resumes after restart without duplicating external objects.
- PR review includes actual file diffs and available CI/test evidence.
- QA sign-off maps each acceptance criterion to execution evidence or a documented exception.
- No plaintext credentials in storage, prompt context, logs, or memory exports.
