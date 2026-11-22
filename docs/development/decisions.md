# INPROOM — Architecture Decision Register

| ID | Decision | Status | Rationale / validation |
|---|---|---|---|
| ADR-001 | Generic versioned process runtime; SDLC/STLC as process packages | Proposed | Avoid domain hard-coding |
| ADR-002 | Documents and system records canonical; vector/graph/Markdown are projections | Proposed | Provenance, rebuildability, deletion compliance |
| ADR-003 | Hybrid search with project/source ACL enforcement | Proposed | Accuracy and least privilege |
| ADR-004 | Durable workflow (Temporal suggested) | Evaluate | Resume workflows, approvals, retries |
| ADR-005 | PostgreSQL + pgvector initially; graph projection may evolve | Evaluate | Cost, simplicity vs graph-query needs |
| ADR-006 | Provider-neutral frontier and local LLM gateway | Proposed | Privacy, portability and budget |
| ADR-007 | User-approved writes to Jira/Confluence by default | Proposed | Human governance and source authority |
| ADR-008 | Deterministic test results and PR checks separate from LLM commentary | Proposed | Trustworthy evidence |
| ADR-009 | SaaS-first with architecture compatible with VPC/on-prem | Evaluate | Commercial deployment flexibility |
| ADR-010 | Kubernetes conditional on scale, not required for first local pilot | Evaluate | Operational simplicity |
| ADR-011 | Encrypted BYOK credentials via key management and tool broker | Proposed | Credential isolation |
| ADR-012 | Immutable approval/audit events, exact proposal-version binding | Proposed | Reproducibility |

## Review checklist
Validate deployment and tenancy model, graph implementation, source-version protocol, Jira/Confluence field mappings, Git provider scope, model-provider API terms, data retention/residency, threat model, test sandbox security, quality evaluation, and ownership of production sign-off. Each approved ADR should record date, decision makers, alternatives and operational consequences.
