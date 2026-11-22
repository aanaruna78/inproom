# INPROOM — High-Level Architecture & Reference Technology Stack

**Status:** Proposed reference design; approvals and operational verification pending.

## Goals and design principles
INPROOM implements a general, configurable process runtime. SDLC and STLC are versioned process packages, not hard-coded workflows. The platform must support new projects and legacy migration, human-controlled publishing, explainable decisions with source citations, data boundaries, customer-selected models and safe tool execution.

## Logical services
| Service | Responsibility | Proposed implementation |
|---|---|---|
| UI & API | Project onboarding, connectors, dashboard, approvals, audit | Next.js / React / TypeScript; FastAPI |
| Identity & tenant context | AuthN, RBAC/ABAC, source ACL propagation | OIDC/Keycloak or managed IdP |
| Project/connector service | Git/Jira/Confluence account linkage and sync jobs | OAuth/app installations, webhooks, queue workers |
| Ingestion | Parse documents, tickets, code, source provenance and revisions | Python workers, content parsers, code tree-sitter |
| Knowledge fabric | Canonical source store, hybrid vector + lexical retrieval, graph relationships, evidence | S3/MinIO; PostgreSQL + pgvector; graph projection |
| Process engine | Durable stage transitions, retries, timeouts, approval holds | Temporal (reference option) |
| Agent runtime | Structured tasks, context bounds, evidence-backed outputs | Python agents + typed JSON schemas |
| Model gateway | Frontier/local adapters, routing, limits, versioning, evaluation | Provider SDK adapters; vLLM/Ollama option |
| Tool execution | Restricted Jira/Confluence/Git/CI/test actions | Short-lived worker credentials, sandbox |
| Governance | Policy evaluation, approver identity, signed action evidence | Policy-as-code + append-only audit |
| Observability | Trace processing, model/tool costs, quality, errors | OpenTelemetry, Prometheus, Grafana |

## Data boundaries
- Each request has tenant ID, workspace/project ID, user identity, entitlements, process version and source ACLs.
- Original documents and revisions are authoritative; graph/vector/Markdown projections are derivations that can be rebuilt.
- Credentials encrypted with KMS, brokered at execution time; never sent as LLM context.
- Connector content is **untrusted input**: it cannot override system rules, approvals or allowed tool calls.
- AI-generated requirements, trace edges and diagrams are proposed evidence until validated or approved.
- Production deployment and irreversible writes are disabled by default.

## Deployment choices
SaaS: isolated logical tenants with mandatory row-level authorization and encrypted storage. Dedicated VPC: segregated data plane. Hybrid: controlled relay to customer systems. On-prem: local models and self-hosted components; document synchronization and upgrades must account for offline installations.

## Quality targets
Durable workflow recovery, source-cited responses, no cross-tenant retrieval, explicit freshness indicators, idempotent publishing, policy enforcement on every write, per-agent cost limits, structured observability, full audit and human takeover.

See [Mermaid diagrams](diagrams.md), [lifecycle](../lifecycle/complete-lifecycle.md), and [technical decisions](../development/decisions.md).