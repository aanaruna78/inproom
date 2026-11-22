# INPROOM — Development Roadmap and MVP Exit Criteria

**Sequence:** shared process runtime → INPROOM Dev SDLC MVP → governed PR/release workflows → INPROOM Test STLC → general process packs.

## Workstream and epic backlog
| Epic | Deliverable | First proof |
|---|---|---|
| P01 | Tenant/project workspace + RBAC | Isolated project create/read/update |
| P02 | Git/Jira/Confluence connectors | Least-privilege access and incremental sync |
| P03 | Canonical source storage + ingestion | Versioned docs and citations |
| P04 | Hybrid vector + graph retrieval | Relevant evidence with ACLs |
| P05 | Markdown project memory | Rebuildable, versioned, source-backed dump |
| P06 | Model gateway (cloud + local) | Same task schema on 2 model backends |
| P07 | Durable orchestration + policy | Pause/resume approval workflow |
| P08 | Requirements agent + Jira drafts | Approval before writing |
| P09 | Sprint readiness + implementation plan | Reviewer-accepted plan with sources |
| P10 | Mermaid render + Confluence versioned publish | Editable source and rendered image |
| P11 | PR verification harness | Evidence-based verdict on actual PR |
| P12 | Release candidate + QA work generation | Approved QA stories linked to RC |
| P13 | QA automation/test harness | Real execution results with evidence |
| P14 | Security, audit and evaluations | No unauthorized writes or cross-tenant exposure |

## Practical increments
**Milestone A:** one project, three connectors, import/sync, citations, graph/vector/Markdown knowledge.
**Milestone B (SDLC MVP):** requirement → approved Jira epic/story → sprint readiness → human-reviewed plan and diagram.
**Milestone C:** controlled code/PR verification and release candidate evidence.
**Milestone D (STLC):** QA story generation, approval, execution, defect loop and sign-off.
**Milestone E:** configurable business workflow package and reusable tool bindings.

## Mandatory exit gates
1. No test can bypass tenant boundaries.
2. Every external write has recorded approval/policy and idempotency metadata.
3. Retrieval and plan quality measured against a reviewer-labeled golden set.
4. Connector failure and retry scenarios leave no duplicate Jira/Confluence actions.
5. QA results are based on executed checks; no fabricated test success.
6. Encryption, secrets isolation, observability, backup/recovery and incident process reviewed before customer pilot.

Delivery dates and effort estimates are intentionally not committed until team size, source integrations and hosting constraints are validated.
