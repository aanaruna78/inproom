# INPROOM

**Intelligent Process Orchestration & Management**  
**by ThinkHiveLabs**

> From Knowledge to Intelligent Execution.

INPROOM is an AI-native, human-governed process lifecycle platform. Its first package supports software development lifecycle (SDLC), followed by software testing lifecycle (STLC), with a common runtime that can later orchestrate other enterprise processes.

**Status:** Product and architecture design baseline. This repository does not yet contain an implemented application.

## Core product lifecycle

1. Create a project (new or existing/migration).
2. Connect authorized Git, Jira, Confluence and document sources.
3. Build a project knowledge fabric: versioned original sources, hybrid vector retrieval, a relationship graph, and Markdown memory snapshots.
4. Analyze requirements and **draft** proposed epics, stories, acceptance criteria and dependencies.
5. After user approval, publish approved work items to Jira.
6. Analyze sprint allocation and prepare evidence-backed implementation plans and Mermaid architecture diagrams. Publish Jira/Confluence updates under policy gates.
7. Allow human development or controlled AI-assisted development; inspect commits, pull requests, CI and tests against acceptance criteria.
8. Assemble a governed release candidate. Generate QA stories, test plans and regression scope for approval.
9. Execute manual, automated or AI-assisted testing. Attach evidence, create defects, obtain QA sign-off and close eligible work items.
10. Trace results back to requirements, code, test evidence, release and incident history, then refresh the knowledge fabric.

## Architecture

INPROOM comprises a shared **Process Definition & Workflow Engine**, **Knowledge Fabric**, **Model Gateway**, **Agent & Tool Runtime**, **Approval/Policy Engine**, and **Audit/Evidence Ledger**. SDLC and STLC are versioned process packages, not separate platforms.

Model access may use approved cloud frontier models, customer-supplied API credentials (BYOK), or private/local LLMs. A model subscription does **not** necessarily grant API access. Private credentials must be encrypted and injected only through a broker, never included in prompts, Markdown memory or logs.

## Documentation

- [Architecture and technology choices](docs/architecture/overview.md)
- [Full SDLC → STLC lifecycle](docs/lifecycle/complete-lifecycle.md)
- [SDLC use cases](docs/use-cases/sdlc.md)
- [STLC use cases](docs/use-cases/stlc.md)
- [Implementation roadmap](docs/development/roadmap.md)
- [Editable Mermaid architecture diagrams](docs/architecture/diagrams.md)
- [Architecture decisions](docs/development/decisions.md)

## Repository conventions

Documentation changes should be reviewed in pull requests, preserving Mermaid sources, evidence and version history. Keep production secrets, API keys, private certificates and customer project data **out of Git**. No automated production changes are permitted by default.

## Product family

- **INPROOM Core:** configurable process orchestration and knowledge services.
- **INPROOM Dev:** SDLC process package (first delivery).
- **INPROOM Test:** STLC and QA automation (second delivery).
- **INPROOM Flow:** future general business processes.

All proposed tools, vendor choices and features remain subject to architectural validation, security review and implementation.
