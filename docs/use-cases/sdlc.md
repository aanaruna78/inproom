# INPROOM Dev — SDLC Use Cases

## UC-DEV-01 Project creation and migration
Actor: Project owner. Inputs: project type, Git repository, Jira project, Confluence space, approved documents. Steps: authenticate connectors → inspect sources → assess indexing scope → create source snapshots → infer entities/links → present completeness report. Outputs: project knowledge version, unresolved migration gaps. Failure: access denied or unsupported source; do not fabricate links.

## UC-DEV-02 Requirement → epic and story drafting
Actor: Product owner. Inputs: source-backed requirement and configured Jira schema. Agent identifies ambiguities, drafts epic, user stories, acceptance criteria, priority suggestions and dependencies. Owner edits and explicitly approves exact payload; system publishes with idempotency tokens. Evidence: source citations, approval, Jira keys. Rejection returns to draft without writes.

## UC-DEV-03 Sprint intelligence
Actor: Team lead. Trigger: sprint selection / Jira updates. Read authorized stories and refreshed KB; assess completeness, architecture impact, dependencies and risks. Output: readiness report, missing information and source links. No auto sprint reassignment unless policy and approval allow.

## UC-DEV-04 Implementation plan and diagrams
Actor: Lead / developer. Input: approved story. Output: scoped step-by-step implementation plan, affected components/APIs, test recommendations, compatibility risks, Mermaid source and rendered diagrams. Publication to Confluence creates a versioned page update after review.

## UC-DEV-05 Human or AI-assisted development
Actor: Developer. Work in an approved branch and path-scoped sandbox. AI harness may propose or implement changes under configured autonomy, run deterministic checks and create a PR. Do not bypass repository branch protection or code-owner review.

## UC-DEV-06 Pull request verification
Actor: Reviewer. Input: PR and linked Jira story/plan. Verify actual diff, protected paths, acceptance criteria, CI/test results and security scanning. Output: evidence-backed structured review with pass / fail / inconclusive per criterion. A passing review never guarantees zero defects.

## UC-DEV-07 Release candidate composition
Actor: Release owner. Assemble approved commits, linked stories, build artifacts, dependency order, regression target and rollback readiness. Publish immutable release candidate and await QA/release gates. Production deploy disabled by default.

## Cross-cutting acceptance
Every output is tenant-scoped, source-cited, auditable and re-evaluated when inputs change. All external writes have explicit permission and idempotency protections. See [lifecycle contract](../lifecycle/complete-lifecycle.md).
