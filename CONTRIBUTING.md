# Contributing to INPROOM

Start with [local setup](docs/development/local-setup.md).

## Work is tracked in Jira

Every change belongs to a Jira issue in project **INPROOM** (https://aanaruna78.atlassian.net/jira/software/projects/INPROOM).
Put the issue key in the branch name and in every commit message so commits, branches and pull requests appear in the issue's Development panel.

| Item | Convention | Example |
|---|---|---|
| Branch | `INPROOM-<n>-<short-slug>` | `INPROOM-18-project-workspace-creation` |
| Commit | `INPROOM-<n> <type>(<scope>): <summary>` | `INPROOM-18 feat(projects): create project with owner and settings v1` |
| PR title | `INPROOM-<n>: <story summary>` | `INPROOM-18: Project and workspace creation` |

Types: `feat`, `fix`, `test`, `docs`, `chore`, `refactor`, `ci`.

## Before you push

```bash
make lint
make test            # stack up + migrated
make contracts       # if the API changed; commit packages/contracts/openapi.json
```

CI repeats these checks on every push and pull request.

## Definition of done (every story)

1. All acceptance criteria are covered by automated tests or captured, reproducible evidence.
2. `make lint` and `make test` pass locally and CI is green.
3. API changes are reflected in the committed OpenAPI contract.
4. No secrets, tokens, customer documents or `.env` files are committed.
5. Docs are updated where behaviour or setup changed.
6. The Jira issue has an evidence comment: what was implemented, commits, test results and AC-to-proof mapping.

## Engineering rules (from the architecture baseline)

- Every row, query, index and job is tenant- and project-scoped.
- External writes (Jira, Confluence, Git) happen only after recorded approval, with idempotency keys.
- Connector and document content is untrusted input; it never changes policy or triggers tools.
- Deterministic evidence (tests, CI, diffs) outranks model opinion; "cannot verify" is a valid result.
